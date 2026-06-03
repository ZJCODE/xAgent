"""Low-level Qwen/DashScope realtime STT/TTS adapters."""
from __future__ import annotations

import base64
import json
import queue
import threading
import time
import uuid
from typing import Any, Iterable, Iterator
from urllib.parse import urlencode

from .config import VoiceChannelConfig, VoiceSTTConfig, VoiceTTSConfig
from .runtime import VoiceUtterance


QWEN_REALTIME_WEBSOCKET_BASE_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
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


class QwenVoiceError(RuntimeError):
    """Raised for Qwen realtime voice errors."""


def _connect_qwen_websocket(url: str, *, api_key: str):
    try:
        from websockets.sync.client import connect  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised by CLI import guard
        raise QwenVoiceError(
            "The voice command requires the WebSocket dependency websockets. "
            "Reinstall or upgrade myxagent, then try again."
        ) from exc
    return connect(
        url,
        additional_headers={
            "Authorization": f"Bearer {api_key}",
            "OpenAI-Beta": "realtime=v1",
        },
    )


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
        send_lock = threading.Lock()
        url = _qwen_realtime_url(model=self.config.model, base_url=self.websocket_base_url)
        with _connect_qwen_websocket(url, api_key=self.api_key) as ws:
            ws.send(_compact_json(self._session_update_event()))
            sender = threading.Thread(
                target=self._send_audio_loop,
                args=(ws, audio_chunks, send_lock, pause_event, stop_event),
                daemon=True,
            )
            sender.start()
            try:
                for message in _iter_json_messages(ws):
                    if stop_event.is_set():
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
                stop_event.set()
                with send_lock:
                    try:
                        ws.send(_compact_json(_event("session.finish")))
                    except Exception:
                        pass
                sender.join(timeout=1.0)

    def _session_update_event(self) -> dict[str, Any]:
        session = {
            "modalities": ["text"],
            "input_audio_format": self.config.audio_format,
            "sample_rate": self.config.sample_rate,
            "turn_detection": {
                "type": self.config.turn_detection,
                "threshold": self.config.vad_threshold,
                "silence_duration_ms": self.config.silence_duration_ms,
            },
        }
        if self.config.language:
            session["input_audio_transcription"] = {
                "language": self.config.language,
            }
        return _event(
            "session.update",
            session=session,
        )

    def _send_audio_loop(
        self,
        ws,  # noqa: ANN001
        audio_chunks: Iterable[bytes],
        send_lock: threading.Lock,
        pause_event: threading.Event,
        stop_event: threading.Event,
    ) -> None:
        try:
            for chunk in audio_chunks:
                if stop_event.is_set():
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
            stop_event.set()

    @staticmethod
    def _raise_if_error(message: dict[str, Any]) -> None:
        if message.get("type") == "error":
            raise QwenVoiceError(f"Qwen STT error: {message.get('error') or message}")


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
                        raise QwenVoiceError(str(send_errors.get()))
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
        session = {
            "mode": self.config.mode,
            "voice": self.config.voice,
            "language_type": _qwen_language_type(language, fallback=self.config.language_type),
            "response_format": self.config.audio_format,
            "sample_rate": self.config.sample_rate,
        }
        if self.config.instructions:
            session["instructions"] = self.config.instructions
            if self.config.optimize_instructions:
                session["optimize_instructions"] = True
        return _event(
            "session.update",
            session=session,
        )

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
        if message.get("type") == "error":
            raise QwenVoiceError(f"Qwen TTS error: {message.get('error') or message}")


def create_qwen_adapters(config: VoiceChannelConfig) -> tuple[QwenRealtimeSTT, QwenRealtimeTTS]:
    api_key = config.resolved_api_key()
    websocket_base_url = config.resolved_websocket_base_url() or QWEN_REALTIME_WEBSOCKET_BASE_URL
    return (
        QwenRealtimeSTT(api_key=api_key, config=config.stt, websocket_base_url=websocket_base_url),
        QwenRealtimeTTS(api_key=api_key, config=config.tts, websocket_base_url=websocket_base_url),
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
