"""Low-level Soniox realtime STT/TTS adapters."""
from __future__ import annotations

import base64
import json
import queue
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Iterator

from .config import VoiceChannelConfig, VoiceSTTConfig, VoiceTTSConfig
from .runtime import VoiceUtterance


SONIOX_STT_WEBSOCKET_URL = "wss://stt-rt.soniox.com/transcribe-websocket"
SONIOX_TTS_WEBSOCKET_URL = "wss://tts-rt.soniox.com/tts-websocket"
KEEPALIVE_INTERVAL_SECONDS = 10.0
TTS_IDLE_FLUSH_SECONDS = 0.25
TEXT_BOUNDARY_SUFFIXES = (".", "!", "?", "\n", "。", "！", "？")


class SonioxVoiceError(RuntimeError):
    """Raised for Soniox realtime voice errors."""


def _connect_websocket(url: str):
    try:
        from websockets.sync.client import connect  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised by CLI import guard
        raise SonioxVoiceError(
            "The voice command requires the WebSocket dependency websockets. "
            "Reinstall or upgrade myxagent, then try again."
        ) from exc
    return connect(url)


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
        send_lock = threading.Lock()
        with _connect_websocket(self.websocket_url) as ws:
            ws.send(_compact_json(self._config_payload()))
            sender = threading.Thread(
                target=self._send_audio_loop,
                args=(ws, audio_chunks, send_lock, pause_event, stop_event),
                daemon=True,
            )
            sender.start()
            final_tokens: list[_FinalToken] = []
            try:
                for message in _iter_json_messages(ws):
                    if stop_event.is_set():
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
                stop_event.set()
                with send_lock:
                    try:
                        ws.send("")
                    except Exception:
                        pass
                sender.join(timeout=1.0)

    def _config_payload(self) -> dict[str, Any]:
        payload = self.config.model_dump(
            exclude={"provider", "language", "turn_detection", "silence_duration_ms"},
            exclude_none=True,
        )
        payload["api_key"] = self.api_key
        return payload

    def _send_audio_loop(
        self,
        ws,  # noqa: ANN001
        audio_chunks: Iterable[bytes],
        send_lock: threading.Lock,
        pause_event: threading.Event,
        stop_event: threading.Event,
    ) -> None:
        last_keepalive_at = time.monotonic()
        try:
            for chunk in audio_chunks:
                if stop_event.is_set():
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
            stop_event.set()

    @staticmethod
    def _tokens(message: dict[str, Any]) -> list[dict[str, Any]]:
        tokens = message.get("tokens")
        return tokens if isinstance(tokens, list) else []

    @staticmethod
    def _raise_if_error(message: dict[str, Any]) -> None:
        error_code = message.get("error_code")
        if error_code:
            error_message = message.get("error_message") or "Soniox realtime STT error"
            raise SonioxVoiceError(f"Soniox STT error {error_code}: {error_message}")

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
                        raise SonioxVoiceError(str(send_errors.get()))
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
        payload = {
            "api_key": self.api_key,
            "stream_id": stream_id,
            "model": self.config.model,
            "language": language,
            "voice": self.config.voice,
            "audio_format": self.config.audio_format,
        }
        if self.config.sample_rate:
            payload["sample_rate"] = self.config.sample_rate
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
        error_code = message.get("error_code")
        if error_code:
            error_message = message.get("error_message") or "Soniox realtime TTS error"
            request_id = message.get("request_id")
            suffix = f" request_id={request_id}" if request_id else ""
            raise SonioxVoiceError(f"Soniox TTS error {error_code}: {error_message}{suffix}")


def create_soniox_adapters(config: VoiceChannelConfig) -> tuple[SonioxRealtimeSTT, SonioxRealtimeTTS]:
    return (
        SonioxRealtimeSTT(api_key=config.resolved_stt_api_key(), config=config.stt),
        SonioxRealtimeTTS(api_key=config.resolved_tts_api_key(), config=config.tts),
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
