"""Low-level Qwen/DashScope realtime STT/TTS adapters."""
from __future__ import annotations

import base64
import json
import logging
import queue
import threading
import time
import uuid
from typing import Any, Iterable, Iterator
from urllib.parse import urlencode

from .config import VoiceChannelConfig, VoiceSTTConfig, VoiceTTSConfig
from .runtime import VoiceUtterance
from .ws_util import (
    STT_RECONNECT_BASE_SECONDS,
    WS_PING_INTERVAL_SECONDS,
    WS_PING_TIMEOUT_SECONDS,
    is_transport_error,
    next_stt_reconnect_delay,
    reraise_send_error,
    wait_for_first_text,
)

_logger = logging.getLogger(__name__)


QWEN_REALTIME_WEBSOCKET_BASE_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
QWEN_REALTIME_WEBSOCKET_INTL_URL = "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime"
TTS_IDLE_FLUSH_SECONDS = 0.25
TEXT_BOUNDARY_SUFFIXES = (".", "!", "?", "\n", "。", "！", "？")
QWEN_LANGUAGE_TYPES_BY_CODE = {
    "zh": "Chinese",
    "en": "English",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "es": "Spanish",
    "ja": "Japanese",
    "ko": "Korean",
    "fr": "French",
    "ru": "Russian",
}
_QWEN_STT_SESSION_OPTION_KEYS = frozenset({
    "input_audio_format",
    "sample_rate",
    "input_audio_transcription",
    "turn_detection",
})
_QWEN_TTS_SESSION_OPTION_KEYS = frozenset({
    "mode",
    "voice",
    "language_type",
    "response_format",
    "sample_rate",
    "speech_rate",
    "volume",
    "pitch_rate",
    "bit_rate",
    "instructions",
    "optimize_instructions",
})


class QwenVoiceError(RuntimeError):
    """Raised for Qwen realtime voice errors."""

    def __init__(
        self,
        message: str,
        *,
        error_type: str | None = None,
        error_code: Any = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.error_code = error_code
        self.request_id = request_id


def _connect_qwen_websocket(url: str, *, api_key: str):
    try:
        from websockets.sync.client import connect  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised by CLI import guard
        raise QwenVoiceError(
            "The voice command requires the WebSocket dependency websockets. "
            "Reinstall or upgrade myxagent, then try again."
        ) from exc
    try:
        return connect(
            url,
            additional_headers={
                "Authorization": f"Bearer {api_key}",
                "OpenAI-Beta": "realtime=v1",
            },
            ping_interval=WS_PING_INTERVAL_SECONDS,
            ping_timeout=WS_PING_TIMEOUT_SECONDS,
        )
    except ImportError as exc:
        raise QwenVoiceError(
            "Qwen realtime voice WebSocket needs python-socks when a SOCKS proxy is configured. "
            "Install or upgrade myxagent dependencies, then try again."
        ) from exc


def _compact_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _event(event_type: str, **payload: Any) -> dict[str, Any]:
    data = {
        "event_id": f"event_{uuid.uuid4().hex}",
        "type": event_type,
    }
    data.update(payload)
    return data


def _iter_json_messages(ws) -> Iterator[dict[str, Any]]:  # noqa: ANN001
    while True:
        raw_message = ws.recv()
        if raw_message is None:
            break
        if isinstance(raw_message, bytes):
            raw_message = raw_message.decode("utf-8")
        if not raw_message:
            continue
        try:
            data = json.loads(raw_message)
        except json.JSONDecodeError as exc:
            raise QwenVoiceError(f"Invalid Qwen WebSocket message: {exc}") from exc
        if isinstance(data, dict):
            yield data


def _qwen_realtime_url(*, model: str, base_url: str = QWEN_REALTIME_WEBSOCKET_BASE_URL) -> str:
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}{urlencode({'model': model})}"


def _qwen_language_type(language: str, *, fallback: str) -> str:
    normalized = str(language or "").strip().lower()
    if not normalized:
        return fallback
    primary_tag = normalized.split("-", 1)[0].split("_", 1)[0]
    return QWEN_LANGUAGE_TYPES_BY_CODE.get(primary_tag, fallback)


def _raise_qwen_error(message: dict[str, Any], *, kind: str) -> None:
    if message.get("type") != "error":
        return
    error = message.get("error")
    error_type: str | None = None
    error_code: Any = None
    error_message: str | None = None
    request_id = message.get("request_id") or message.get("event_id")
    if isinstance(error, dict):
        error_type = str(error.get("type") or error.get("code") or "") or None
        error_code = error.get("code") if error.get("code") is not None else error.get("type")
        error_message = str(error.get("message") or error.get("msg") or "").strip() or None
        request_id = error.get("request_id") or error.get("param") or request_id
    elif error is not None:
        error_message = str(error)
    parts = [f"Qwen {kind} error"]
    if error_code is not None:
        parts.append(str(error_code))
    if error_type and str(error_type) != str(error_code):
        parts.append(f"({error_type})")
    detail = error_message or str(error or message)
    text = f"{' '.join(parts)}: {detail}"
    if request_id:
        text = f"{text} request_id={request_id}"
    raise QwenVoiceError(
        text,
        error_type=error_type,
        error_code=error_code,
        request_id=str(request_id) if request_id else None,
    )


def _filter_session_options(options: dict[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    return {key: value for key, value in options.items() if key in allowed}


class QwenRealtimeSTT:
    """Stream local audio to Qwen-ASR Realtime and yield completed transcripts."""

    def __init__(
        self,
        *,
        api_key: str,
        config: VoiceSTTConfig,
        websocket_base_url: str = QWEN_REALTIME_WEBSOCKET_BASE_URL,
    ) -> None:
        self.api_key = api_key
        self.config = config
        self.websocket_base_url = websocket_base_url

    def iter_utterances(
        self,
        audio_chunks: Iterable[bytes],
        *,
        pause_event: threading.Event,
        stop_event: threading.Event,
    ) -> Iterator[VoiceUtterance]:
        """Yield utterances across STT reconnects until the runtime stop_event is set."""
        backoff = STT_RECONNECT_BASE_SECONDS
        while not stop_event.is_set():
            session_stop = threading.Event()
            produced_utterance = False
            try:
                for utterance in self._iter_utterances_session(
                    audio_chunks,
                    pause_event=pause_event,
                    stop_event=stop_event,
                    session_stop=session_stop,
                ):
                    produced_utterance = True
                    backoff = STT_RECONNECT_BASE_SECONDS
                    yield utterance
            except Exception as exc:
                if stop_event.is_set():
                    return
                if not is_transport_error(exc):
                    raise
                _logger.warning("Qwen STT session failed (%s); reconnecting in %.1fs", exc, backoff)
            else:
                if stop_event.is_set():
                    return
                _logger.warning("Qwen STT session ended; reconnecting in %.1fs", backoff)
            finally:
                session_stop.set()

            if stop_event.is_set():
                return
            if produced_utterance:
                backoff = STT_RECONNECT_BASE_SECONDS
            time.sleep(backoff)
            backoff = next_stt_reconnect_delay(backoff)

    def _iter_utterances_session(
        self,
        audio_chunks: Iterable[bytes],
        *,
        pause_event: threading.Event,
        stop_event: threading.Event,
        session_stop: threading.Event,
    ) -> Iterator[VoiceUtterance]:
        send_lock = threading.Lock()
        url = _qwen_realtime_url(model=self.config.model, base_url=self.websocket_base_url)
        with _connect_qwen_websocket(url, api_key=self.api_key) as ws:
            ws.send(_compact_json(self._session_update_event()))
            sender = threading.Thread(
                target=self._send_audio_loop,
                args=(ws, audio_chunks, send_lock, pause_event, stop_event, session_stop),
                daemon=True,
            )
            sender.start()
            try:
                for message in _iter_json_messages(ws):
                    if stop_event.is_set() or session_stop.is_set():
                        break
                    self._raise_if_error(message)
                    event_type = str(message.get("type") or "")
                    if event_type == "conversation.item.input_audio_transcription.completed":
                        transcript = str(message.get("transcript") or "").strip()
                        if transcript:
                            yield VoiceUtterance(text=transcript, language=self.config.language)
                    if event_type == "session.finished":
                        break
            finally:
                session_stop.set()
                with send_lock:
                    try:
                        ws.send(_compact_json(_event("session.finish")))
                    except Exception:
                        pass
                sender.join(timeout=1.0)

    def _session_update_event(self) -> dict[str, Any]:
        transcription: dict[str, Any] = {}
        if self.config.language:
            transcription["language"] = self.config.language
        if self.config.corpus_text:
            transcription["corpus"] = {"text": self.config.corpus_text}

        session = _filter_session_options(
            dict(self.config.session_options),
            _QWEN_STT_SESSION_OPTION_KEYS,
        )
        session.update({
            "input_audio_format": self.config.audio_format,
            "sample_rate": self.config.sample_rate,
            "turn_detection": {
                "type": self.config.turn_detection,
                "threshold": self.config.vad_threshold,
                "silence_duration_ms": self.config.silence_duration_ms,
            },
        })
        if transcription:
            existing = session.get("input_audio_transcription")
            if isinstance(existing, dict):
                merged = dict(existing)
                merged.update(transcription)
                session["input_audio_transcription"] = merged
            else:
                session["input_audio_transcription"] = transcription
        return _event("session.update", session=session)

    def _send_audio_loop(
        self,
        ws,  # noqa: ANN001
        audio_chunks: Iterable[bytes],
        send_lock: threading.Lock,
        pause_event: threading.Event,
        stop_event: threading.Event,
        session_stop: threading.Event,
    ) -> None:
        try:
            for chunk in audio_chunks:
                if stop_event.is_set() or session_stop.is_set():
                    break
                if pause_event.is_set():
                    time.sleep(0.05)
                    continue
                if not chunk:
                    continue
                payload = _event(
                    "input_audio_buffer.append",
                    audio=base64.b64encode(chunk).decode("ascii"),
                )
                with send_lock:
                    ws.send(_compact_json(payload))
        except Exception:
            session_stop.set()

    @staticmethod
    def _raise_if_error(message: dict[str, Any]) -> None:
        _raise_qwen_error(message, kind="STT")


class QwenRealtimeTTS:
    """Generate speech from assistant text using Qwen-TTS Realtime."""

    def __init__(
        self,
        *,
        api_key: str,
        config: VoiceTTSConfig,
        websocket_base_url: str = QWEN_REALTIME_WEBSOCKET_BASE_URL,
    ) -> None:
        self.api_key = api_key
        self.config = config
        self.websocket_base_url = websocket_base_url
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def synthesize_chunks(
        self,
        text_chunks: Iterable[str],
        *,
        language: str,
        stop_event: threading.Event,
    ) -> Iterator[bytes]:
        self._cancel_event.clear()
        # Qwen has no app keepalive and idle-disconnects; wait for text before connecting.
        source = wait_for_first_text(
            text_chunks,
            stop_event=stop_event,
            cancel_event=self._cancel_event,
        )
        if source is None:
            return

        attempts = 2
        for attempt in range(attempts):
            produced_audio = False
            try:
                for audio in self._synthesize_once(
                    source,
                    language=language,
                    stop_event=stop_event,
                ):
                    if not produced_audio:
                        produced_audio = True
                        source.commit()
                    yield audio
                return
            except Exception as exc:
                if (
                    produced_audio
                    or attempt + 1 >= attempts
                    or not is_transport_error(exc)
                    or self._cancel_event.is_set()
                    or stop_event.is_set()
                ):
                    raise
                source.rewind()
                time.sleep(0.35 * (attempt + 1))

    def _synthesize_once(
        self,
        text_chunks: Iterable[str],
        *,
        language: str,
        stop_event: threading.Event,
    ) -> Iterator[bytes]:
        send_errors: "queue.Queue[BaseException]" = queue.Queue()
        url = _qwen_realtime_url(model=self.config.model, base_url=self.websocket_base_url)
        with _connect_qwen_websocket(url, api_key=self.api_key) as ws:
            ws.send(_compact_json(self._session_update_event(language=language)))
            sender = threading.Thread(
                target=self._send_text_loop,
                args=(ws, text_chunks, send_errors, stop_event),
                daemon=True,
            )
            sender.start()
            cancel_sent = False
            try:
                for message in _iter_json_messages(ws):
                    self._raise_if_error(message)
                    if not send_errors.empty():
                        reraise_send_error(send_errors.get(), error_cls=QwenVoiceError)
                    event_type = str(message.get("type") or "")
                    if (self._cancel_event.is_set() or stop_event.is_set()) and not cancel_sent:
                        self._send_cancel(ws)
                        cancel_sent = True
                    if event_type == "response.audio.delta":
                        audio = message.get("delta")
                        if isinstance(audio, str) and audio:
                            yield base64.b64decode(audio)
                    if event_type == "session.finished":
                        break
            finally:
                sender.join(timeout=1.0)

    def _session_update_event(self, *, language: str) -> dict[str, Any]:
        session = _filter_session_options(
            dict(self.config.session_options),
            _QWEN_TTS_SESSION_OPTION_KEYS,
        )
        session.update({
            "mode": self.config.mode,
            "voice": self.config.voice,
            "language_type": _qwen_language_type(language, fallback=self.config.language_type),
            "response_format": self.config.audio_format,
            "sample_rate": self.config.sample_rate,
        })
        if abs(self.config.speech_rate - 1.0) > 1e-9:
            session["speech_rate"] = self.config.speech_rate
        if self.config.volume != 50:
            session["volume"] = self.config.volume
        if abs(self.config.pitch_rate - 1.0) > 1e-9:
            session["pitch_rate"] = self.config.pitch_rate
        if self.config.instructions:
            session["instructions"] = self.config.instructions
            if self.config.optimize_instructions:
                session["optimize_instructions"] = True
        return _event("session.update", session=session)

    def _send_text_loop(
        self,
        ws,  # noqa: ANN001
        text_chunks: Iterable[str],
        send_errors: "queue.Queue[BaseException]",
        stop_event: threading.Event,
    ) -> None:
        try:
            next_item = getattr(text_chunks, "next_item", None)
            if callable(next_item):
                self._send_timeout_aware_text_loop(ws, next_item, stop_event)
                return
            for chunk in _batch_text_chunks(text_chunks, max_chars=self.config.max_buffer_chars):
                if self._cancel_event.is_set() or stop_event.is_set():
                    self._send_cancel(ws)
                    return
                self._send_text_chunk(ws, chunk)
            self._send_finish(ws)
        except Exception as exc:
            send_errors.put(exc)

    def _send_timeout_aware_text_loop(
        self,
        ws,  # noqa: ANN001
        next_item,  # noqa: ANN001
        stop_event: threading.Event,
    ) -> None:
        buffer = ""
        while not self._cancel_event.is_set() and not stop_event.is_set():
            try:
                chunk = next_item(TTS_IDLE_FLUSH_SECONDS)
            except StopIteration:
                break
            if chunk is None:
                if buffer:
                    self._send_text_chunk(ws, buffer)
                    buffer = ""
                continue
            buffer += chunk
            if len(buffer) >= self.config.max_buffer_chars or buffer.endswith(TEXT_BOUNDARY_SUFFIXES):
                self._send_text_chunk(ws, buffer)
                buffer = ""

        if self._cancel_event.is_set() or stop_event.is_set():
            self._send_cancel(ws)
            return
        if buffer:
            self._send_text_chunk(ws, buffer)
        self._send_finish(ws)

    def _send_text_chunk(self, ws, text: str) -> None:  # noqa: ANN001
        if not text:
            return
        ws.send(_compact_json(_event("input_text_buffer.append", text=text)))
        if self.config.mode == "commit":
            ws.send(_compact_json(_event("input_text_buffer.commit")))

    @staticmethod
    def _send_finish(ws) -> None:  # noqa: ANN001
        ws.send(_compact_json(_event("session.finish")))

    @staticmethod
    def _send_cancel(ws) -> None:  # noqa: ANN001
        try:
            ws.send(_compact_json(_event("input_text_buffer.clear")))
            ws.send(_compact_json(_event("session.finish")))
        except Exception:
            pass

    @staticmethod
    def _raise_if_error(message: dict[str, Any]) -> None:
        _raise_qwen_error(message, kind="TTS")


def create_qwen_adapters(config: VoiceChannelConfig) -> tuple[QwenRealtimeSTT, QwenRealtimeTTS]:
    websocket_base_url = config.resolved_websocket_base_url() or QWEN_REALTIME_WEBSOCKET_BASE_URL
    return (
        QwenRealtimeSTT(
            api_key=config.resolved_stt_api_key(),
            config=config.stt,
            websocket_base_url=websocket_base_url,
        ),
        QwenRealtimeTTS(
            api_key=config.resolved_tts_api_key(),
            config=config.tts,
            websocket_base_url=websocket_base_url,
        ),
    )


def _batch_text_chunks(text_chunks: Iterable[str], *, max_chars: int) -> Iterator[str]:
    buffer = ""
    for chunk in text_chunks:
        if not chunk:
            continue
        buffer += chunk
        if len(buffer) >= max_chars or buffer.endswith(TEXT_BOUNDARY_SUFFIXES):
            yield buffer
            buffer = ""
    if buffer:
        yield buffer
