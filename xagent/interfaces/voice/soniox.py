"""Low-level Soniox realtime STT/TTS adapters."""
from __future__ import annotations

import base64
import json
import logging
import queue
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Iterator

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


SONIOX_STT_WEBSOCKET_URL = "wss://stt-rt.soniox.com/transcribe-websocket"
SONIOX_TTS_WEBSOCKET_URL = "wss://tts-rt.soniox.com/tts-websocket"
KEEPALIVE_INTERVAL_SECONDS = 10.0
TTS_IDLE_FLUSH_SECONDS = 0.25
TEXT_BOUNDARY_SUFFIXES = (".", "!", "?", "\n", "。", "！", "？")
_RETRYABLE_ERROR_TYPES = frozenset({
    "service_unavailable",
    "request_timeout",
    "limit_exceeded",
    "internal_error",
})
_SONIOX_STT_PAYLOAD_KEYS = (
    "model",
    "audio_format",
    "sample_rate",
    "num_channels",
    "language_hints",
    "language_hints_strict",
    "enable_endpoint_detection",
    "max_endpoint_delay_ms",
    "endpoint_sensitivity",
    "endpoint_latency_adjustment_level",
    "enable_language_identification",
    "enable_speaker_diarization",
    "client_reference_id",
    "translation",
)


class SonioxVoiceError(RuntimeError):
    """Raised for Soniox realtime voice errors."""

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

    @property
    def retryable(self) -> bool:
        return bool(self.error_type and self.error_type in _RETRYABLE_ERROR_TYPES)


def _connect_websocket(url: str):
    try:
        from websockets.sync.client import connect  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised by CLI import guard
        raise SonioxVoiceError(
            "The voice command requires the WebSocket dependency websockets. "
            "Reinstall or upgrade myxagent, then try again."
        ) from exc
    try:
        return connect(
            url,
            ping_interval=WS_PING_INTERVAL_SECONDS,
            ping_timeout=WS_PING_TIMEOUT_SECONDS,
        )
    except ImportError as exc:
        raise SonioxVoiceError(
            "Soniox realtime voice WebSocket needs python-socks when a SOCKS proxy is configured. "
            "Install or upgrade myxagent dependencies, then try again."
        ) from exc


def _compact_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


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
            raise SonioxVoiceError(f"Invalid Soniox WebSocket message: {exc}") from exc
        if not isinstance(data, dict):
            continue
        yield data


def _raise_soniox_error(message: dict[str, Any], *, kind: str) -> None:
    error_code = message.get("error_code")
    if not error_code:
        return
    error_type = str(message.get("error_type") or "unknown_error")
    error_message = message.get("error_message") or f"Soniox realtime {kind} error"
    request_id = message.get("request_id")
    parts = [f"Soniox {kind} error {error_code} ({error_type}): {error_message}"]
    if request_id:
        parts.append(f"request_id={request_id}")
    raise SonioxVoiceError(
        " ".join(parts),
        error_type=error_type,
        error_code=error_code,
        request_id=str(request_id) if request_id else None,
    )


@dataclass(frozen=True)
class _FinalToken:
    text: str
    language: str = ""


class SonioxRealtimeSTT:
    """Stream local audio to Soniox and yield finalized utterances at `<end>`."""

    def __init__(
        self,
        *,
        api_key: str,
        config: VoiceSTTConfig,
        websocket_url: str = SONIOX_STT_WEBSOCKET_URL,
    ) -> None:
        self.api_key = api_key
        self.config = config
        self.websocket_url = websocket_url

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
                reconnectable = is_transport_error(exc) or (
                    isinstance(exc, SonioxVoiceError) and exc.retryable
                )
                if not reconnectable:
                    raise
                _logger.warning("Soniox STT session failed (%s); reconnecting in %.1fs", exc, backoff)
            else:
                if stop_event.is_set():
                    return
                _logger.warning("Soniox STT session ended; reconnecting in %.1fs", backoff)
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
        with _connect_websocket(self.websocket_url) as ws:
            ws.send(_compact_json(self._config_payload()))
            sender = threading.Thread(
                target=self._send_audio_loop,
                args=(ws, audio_chunks, send_lock, pause_event, stop_event, session_stop),
                daemon=True,
            )
            sender.start()
            final_tokens: list[_FinalToken] = []
            try:
                for message in _iter_json_messages(ws):
                    if stop_event.is_set() or session_stop.is_set():
                        break
                    self._raise_if_error(message)
                    for token in self._tokens(message):
                        text = str(token.get("text") or "")
                        if not token.get("is_final"):
                            continue
                        if text == "<end>":
                            utterance = self._utterance_from(final_tokens)
                            final_tokens = []
                            if utterance.text.strip():
                                yield utterance
                            continue
                        final_tokens.append(
                            _FinalToken(text=text, language=str(token.get("language") or ""))
                        )
                    if message.get("finished"):
                        break
            finally:
                session_stop.set()
                with send_lock:
                    try:
                        ws.send("")
                    except Exception:
                        pass
                sender.join(timeout=1.0)

    def _config_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"api_key": self.api_key}
        raw = self.config.model_dump(exclude_none=True)
        for key in _SONIOX_STT_PAYLOAD_KEYS:
            if key not in raw:
                continue
            value = raw[key]
            if key == "language_hints_strict" and not value:
                continue
            if key == "enable_speaker_diarization" and not value:
                continue
            payload[key] = value
        if self.config.context is not None:
            context_payload = self.config.context.to_soniox_payload()
            if context_payload:
                payload["context"] = context_payload
        return payload

    def _send_audio_loop(
        self,
        ws,  # noqa: ANN001
        audio_chunks: Iterable[bytes],
        send_lock: threading.Lock,
        pause_event: threading.Event,
        stop_event: threading.Event,
        session_stop: threading.Event,
    ) -> None:
        last_keepalive_at = time.monotonic()
        try:
            for chunk in audio_chunks:
                if stop_event.is_set() or session_stop.is_set():
                    break
                if pause_event.is_set():
                    now = time.monotonic()
                    if now - last_keepalive_at >= KEEPALIVE_INTERVAL_SECONDS:
                        with send_lock:
                            ws.send(_compact_json({"type": "keepalive"}))
                        last_keepalive_at = now
                    time.sleep(0.05)
                    continue
                if not chunk:
                    continue
                with send_lock:
                    ws.send(chunk)
                last_keepalive_at = time.monotonic()
        except Exception:
            session_stop.set()

    @staticmethod
    def _tokens(message: dict[str, Any]) -> list[dict[str, Any]]:
        tokens = message.get("tokens")
        return tokens if isinstance(tokens, list) else []

    @staticmethod
    def _raise_if_error(message: dict[str, Any]) -> None:
        _raise_soniox_error(message, kind="STT")

    @staticmethod
    def _utterance_from(tokens: list[_FinalToken]) -> VoiceUtterance:
        text = "".join(token.text for token in tokens).strip()
        languages = Counter(token.language for token in tokens if token.language)
        language = languages.most_common(1)[0][0] if languages else ""
        return VoiceUtterance(text=text, language=language)


class SonioxRealtimeTTS:
    """Generate speech from assistant text using Soniox realtime TTS."""

    def __init__(
        self,
        *,
        api_key: str,
        config: VoiceTTSConfig,
        websocket_url: str = SONIOX_TTS_WEBSOCKET_URL,
    ) -> None:
        self.api_key = api_key
        self.config = config
        self.websocket_url = websocket_url
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
        # Delay connect until first text so idle LLM/tool waits do not hold the socket.
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
                    or not self._is_retryable(exc)
                    or self._cancel_event.is_set()
                    or stop_event.is_set()
                ):
                    raise
                source.rewind()
                time.sleep(0.35 * (attempt + 1))

    @staticmethod
    def _is_retryable(exc: BaseException) -> bool:
        if is_transport_error(exc):
            return True
        return isinstance(exc, SonioxVoiceError) and exc.retryable

    def _synthesize_once(
        self,
        text_chunks: Iterable[str],
        *,
        language: str,
        stop_event: threading.Event,
    ) -> Iterator[bytes]:
        stream_id = f"xagent-tts-{uuid.uuid4().hex}"
        send_errors: "queue.Queue[BaseException]" = queue.Queue()
        with _connect_websocket(self.websocket_url) as ws:
            ws.send(_compact_json(self._config_payload(stream_id=stream_id, language=language)))
            sender = threading.Thread(
                target=self._send_text_loop,
                args=(ws, text_chunks, stream_id, send_errors, stop_event),
                daemon=True,
            )
            sender.start()
            try:
                for message in _iter_json_messages(ws):
                    self._raise_if_error(message)
                    if not send_errors.empty():
                        reraise_send_error(send_errors.get(), error_cls=SonioxVoiceError)
                    if self._cancel_event.is_set() or stop_event.is_set():
                        self._send_cancel(ws, stream_id)
                    audio = message.get("audio")
                    if isinstance(audio, str) and audio:
                        yield base64.b64decode(audio)
                    if message.get("terminated"):
                        break
            finally:
                sender.join(timeout=1.0)

    def _config_payload(self, *, stream_id: str, language: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "api_key": self.api_key,
            "stream_id": stream_id,
            "model": self.config.model,
            "language": language,
            "voice": self.config.voice,
            "audio_format": self.config.audio_format,
        }
        if self.config.sample_rate:
            payload["sample_rate"] = self.config.sample_rate
        if abs(self.config.speed - 1.0) > 1e-9:
            payload["speed"] = self.config.speed
        if self.config.return_timestamps:
            payload["return_timestamps"] = True
        if self.config.client_reference_id:
            payload["client_reference_id"] = self.config.client_reference_id
        return payload

    def _send_text_loop(
        self,
        ws,  # noqa: ANN001
        text_chunks: Iterable[str],
        stream_id: str,
        send_errors: "queue.Queue[BaseException]",
        stop_event: threading.Event,
    ) -> None:
        try:
            next_item = getattr(text_chunks, "next_item", None)
            if callable(next_item):
                self._send_timeout_aware_text_loop(
                    ws,
                    next_item,
                    stream_id,
                    stop_event,
                )
                return

            for chunk in _batch_text_chunks(text_chunks, max_chars=self.config.max_buffer_chars):
                if self._cancel_event.is_set() or stop_event.is_set():
                    self._send_cancel(ws, stream_id)
                    return
                if not chunk:
                    continue
                ws.send(_compact_json({
                    "stream_id": stream_id,
                    "text": chunk,
                    "text_end": False,
                }))
            ws.send(_compact_json({
                "stream_id": stream_id,
                "text": "",
                "text_end": True,
            }))
        except Exception as exc:
            send_errors.put(exc)

    def _send_timeout_aware_text_loop(
        self,
        ws,  # noqa: ANN001
        next_item,  # noqa: ANN001
        stream_id: str,
        stop_event: threading.Event,
    ) -> None:
        buffer = ""
        last_keepalive_at = time.monotonic()
        while not self._cancel_event.is_set() and not stop_event.is_set():
            try:
                chunk = next_item(TTS_IDLE_FLUSH_SECONDS)
            except StopIteration:
                break
            if chunk is None:
                if buffer:
                    self._send_text_chunk(ws, stream_id, buffer, text_end=False)
                    buffer = ""
                    continue
                now = time.monotonic()
                if now - last_keepalive_at >= KEEPALIVE_INTERVAL_SECONDS:
                    ws.send(_compact_json({"keep_alive": True}))
                    last_keepalive_at = now
                continue
            buffer += chunk
            if len(buffer) >= self.config.max_buffer_chars or buffer.endswith(TEXT_BOUNDARY_SUFFIXES):
                self._send_text_chunk(ws, stream_id, buffer, text_end=False)
                buffer = ""
                last_keepalive_at = time.monotonic()

        if self._cancel_event.is_set() or stop_event.is_set():
            self._send_cancel(ws, stream_id)
            return
        if buffer:
            self._send_text_chunk(ws, stream_id, buffer, text_end=False)
        self._send_text_chunk(ws, stream_id, "", text_end=True)

    @staticmethod
    def _send_text_chunk(ws, stream_id: str, text: str, *, text_end: bool) -> None:  # noqa: ANN001
        ws.send(_compact_json({
            "stream_id": stream_id,
            "text": text,
            "text_end": text_end,
        }))

    @staticmethod
    def _send_cancel(ws, stream_id: str) -> None:  # noqa: ANN001
        try:
            ws.send(_compact_json({"stream_id": stream_id, "cancel": True}))
        except Exception:
            pass

    @staticmethod
    def _raise_if_error(message: dict[str, Any]) -> None:
        _raise_soniox_error(message, kind="TTS")


def create_soniox_adapters(config: VoiceChannelConfig) -> tuple[SonioxRealtimeSTT, SonioxRealtimeTTS]:
    tts_config = config.tts
    if config.enable_interruptions and not tts_config.return_timestamps:
        tts_config = tts_config.model_copy(update={"return_timestamps": True})
    return (
        SonioxRealtimeSTT(api_key=config.resolved_stt_api_key(), config=config.stt),
        SonioxRealtimeTTS(api_key=config.resolved_tts_api_key(), config=tts_config),
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
