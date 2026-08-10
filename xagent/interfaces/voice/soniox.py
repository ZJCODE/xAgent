"""Soniox realtime STT/TTS adapters built on the official Python SDK."""
from __future__ import annotations

import logging
import queue
import threading
import uuid
from collections import Counter
from dataclasses import dataclass
from itertools import chain
from typing import Any, Iterable, Iterator

from soniox import SonioxClient
from soniox.types import RealtimeSTTConfig, RealtimeTTSConfig

from .config import (
    SONIOX_AUDIO_FORMAT,
    SONIOX_ENDPOINT_LATENCY_LEVEL,
    SONIOX_ENDPOINT_SENSITIVITY,
    SONIOX_MAX_ENDPOINT_DELAY_MS,
    SONIOX_STT_CHANNELS,
    SONIOX_STT_MODEL,
    SONIOX_STT_SAMPLE_RATE,
    SONIOX_TTS_MAX_TEXT_CHARS,
    SONIOX_TTS_MODEL,
    SONIOX_TTS_SAMPLE_RATE,
    VoiceChannelConfig,
)
from .runtime import VoiceUtterance

_logger = logging.getLogger(__name__)

STT_RECONNECT_BASE_SECONDS = 0.5
STT_RECONNECT_MAX_SECONDS = 30.0
_RETRYABLE_ERROR_TYPES = frozenset({"service_unavailable", "max_duration_reached"})
_NON_RETRYABLE_STATUS_CODES = frozenset({400, 401, 402, 403, 404, 409, 422})


class SonioxVoiceError(RuntimeError):
    """A Soniox error with enough classification for STT recovery."""

    def __init__(
        self,
        message: str,
        *,
        error_type: str | None = None,
        error_code: Any = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.error_code = error_code

    @property
    def retryable(self) -> bool:
        return self.error_type in _RETRYABLE_ERROR_TYPES


@dataclass(frozen=True)
class _FinalToken:
    text: str
    language: str = ""


class SonioxRealtimeSTT:
    """Keep one Soniox STT session open and emit finalized endpoint turns."""

    def __init__(
        self,
        *,
        api_key: str,
        config: VoiceChannelConfig,
        client: Any | None = None,
    ) -> None:
        self.api_key = api_key
        self.config = config
        self._client = client or SonioxClient(api_key=api_key)

    def iter_utterances(
        self,
        audio_chunks: Iterable[bytes],
        *,
        pause_event: threading.Event,
        stop_event: threading.Event,
    ) -> Iterator[VoiceUtterance]:
        """Yield turns across recoverable session failures until stopped."""
        backoff = STT_RECONNECT_BASE_SECONDS
        while not stop_event.is_set():
            session_stop = threading.Event()
            produced_utterance = False
            try:
                for utterance in self._iter_session(
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
                if not _is_recoverable_stt_error(exc):
                    raise
                _logger.warning(
                    "Soniox STT session failed (%s); reconnecting in %.1fs",
                    exc,
                    backoff,
                )
            else:
                if stop_event.is_set():
                    return
                _logger.warning("Soniox STT session ended; reconnecting in %.1fs", backoff)
            finally:
                session_stop.set()

            if stop_event.wait(backoff):
                return
            if produced_utterance:
                backoff = STT_RECONNECT_BASE_SECONDS
            else:
                backoff = min(backoff * 2.0, STT_RECONNECT_MAX_SECONDS)

    def _iter_session(
        self,
        audio_chunks: Iterable[bytes],
        *,
        pause_event: threading.Event,
        stop_event: threading.Event,
        session_stop: threading.Event,
    ) -> Iterator[VoiceUtterance]:
        send_errors: queue.Queue[BaseException] = queue.Queue()
        session = self._client.realtime.stt.connect(config=self._stt_config())
        with session:
            sender = threading.Thread(
                target=self._send_audio_loop,
                args=(
                    session,
                    audio_chunks,
                    pause_event,
                    stop_event,
                    session_stop,
                    send_errors,
                ),
                daemon=True,
                name="xagent-soniox-stt-send",
            )
            sender.start()
            final_tokens: list[_FinalToken] = []
            try:
                for event in session.receive_events():
                    if stop_event.is_set() or session_stop.is_set():
                        break
                    _raise_event_error(event, kind="STT")
                    for token in event.tokens:
                        if not token.is_final:
                            continue
                        text = str(token.text or "")
                        if text == "<end>":
                            utterance = _utterance_from(final_tokens)
                            final_tokens = []
                            if utterance.text:
                                yield utterance
                            continue
                        if text in {"<fin>"}:
                            continue
                        final_tokens.append(
                            _FinalToken(text=text, language=str(token.language or ""))
                        )
                    if event.finished:
                        break
                if not send_errors.empty():
                    raise send_errors.get()
            finally:
                session_stop.set()
                try:
                    session.close()
                except Exception:
                    pass
                sender.join(timeout=1.0)

    def _stt_config(self) -> RealtimeSTTConfig:
        return RealtimeSTTConfig(
            model=SONIOX_STT_MODEL,
            audio_format=SONIOX_AUDIO_FORMAT,
            sample_rate=SONIOX_STT_SAMPLE_RATE,
            num_channels=SONIOX_STT_CHANNELS,
            language_hints=self.config.language_hints,
            context=self.config.context.to_soniox_payload(),
            enable_endpoint_detection=True,
            endpoint_latency_adjustment_level=SONIOX_ENDPOINT_LATENCY_LEVEL,
            endpoint_sensitivity=SONIOX_ENDPOINT_SENSITIVITY,
            max_endpoint_delay_ms=SONIOX_MAX_ENDPOINT_DELAY_MS,
            enable_language_identification=True,
            enable_speaker_diarization=False,
        )

    @staticmethod
    def _send_audio_loop(
        session: Any,
        audio_chunks: Iterable[bytes],
        pause_event: threading.Event,
        stop_event: threading.Event,
        session_stop: threading.Event,
        send_errors: queue.Queue[BaseException],
    ) -> None:
        paused = False
        try:
            for chunk in audio_chunks:
                if stop_event.is_set() or session_stop.is_set():
                    return
                should_pause = pause_event.is_set()
                if should_pause and not paused:
                    session.pause(finalize=False)
                    paused = True
                elif not should_pause and paused:
                    session.resume()
                    paused = False
                if not paused and chunk:
                    session.send_byte_chunk(chunk)
        except Exception as exc:
            if not stop_event.is_set() and not session_stop.is_set():
                send_errors.put(exc)
            session_stop.set()
            try:
                session.close()
            except Exception:
                pass


class SonioxRealtimeTTS:
    """Generate one short Soniox realtime TTS stream per assistant turn."""

    def __init__(
        self,
        *,
        api_key: str,
        config: VoiceChannelConfig,
        client: Any | None = None,
    ) -> None:
        self.api_key = api_key
        self.config = config
        self._client = client or SonioxClient(api_key=api_key)
        self._cancel_event = threading.Event()
        self._connection_lock = threading.Lock()
        self._active_connection: Any | None = None

    def cancel(self) -> None:
        self._cancel_event.set()
        with self._connection_lock:
            connection = self._active_connection
        if connection is not None:
            try:
                connection.cancel()
            except Exception:
                pass

    def synthesize_chunks(
        self,
        text_chunks: Iterable[str],
        *,
        language: str,
        stop_event: threading.Event,
    ) -> Iterator[bytes]:
        self._cancel_event.clear()
        source = iter(text_chunks)
        first_text = next((chunk for chunk in source if chunk), None)
        if first_text is None:
            return
        stream_id = f"xagent-tts-{uuid.uuid4().hex}"
        sdk_config = RealtimeTTSConfig(
            stream_id=stream_id,
            model=SONIOX_TTS_MODEL,
            language=language,
            voice=self.config.voice,
            audio_format=SONIOX_AUDIO_FORMAT,
            sample_rate=SONIOX_TTS_SAMPLE_RATE,
            speed=self.config.speed,
        )
        connection = self._client.realtime.tts.connect(config=sdk_config)
        send_errors: queue.Queue[BaseException] = queue.Queue()
        with connection:
            with self._connection_lock:
                self._active_connection = connection
            sender = threading.Thread(
                target=self._send_text_loop,
                args=(connection, chain((first_text,), source), stop_event, send_errors),
                daemon=True,
                name="xagent-soniox-tts-send",
            )
            sender.start()
            try:
                for audio in connection.receive_audio_chunks():
                    if not send_errors.empty():
                        raise send_errors.get()
                    if audio:
                        yield audio
                if not send_errors.empty():
                    raise send_errors.get()
            finally:
                if self._cancel_event.is_set() or stop_event.is_set():
                    try:
                        connection.cancel()
                    except Exception:
                        pass
                try:
                    connection.close()
                except Exception:
                    pass
                sender.join(timeout=1.0)
                with self._connection_lock:
                    if self._active_connection is connection:
                        self._active_connection = None

    def _send_text_loop(
        self,
        connection: Any,
        text_chunks: Iterable[str],
        stop_event: threading.Event,
        send_errors: queue.Queue[BaseException],
    ) -> None:
        try:
            for chunk in text_chunks:
                if self._cancel_event.is_set() or stop_event.is_set():
                    connection.cancel()
                    return
                for part in _split_text_chunk(chunk):
                    connection.send_text_chunk(part, text_end=False)
            if self._cancel_event.is_set() or stop_event.is_set():
                connection.cancel()
                return
            connection.finish()
        except Exception as exc:
            send_errors.put(exc)
            try:
                connection.close()
            except Exception:
                pass


def create_soniox_adapters(
    config: VoiceChannelConfig,
) -> tuple[SonioxRealtimeSTT, SonioxRealtimeTTS]:
    api_key = config.resolved_api_key()
    client = SonioxClient(api_key=api_key)
    return (
        SonioxRealtimeSTT(api_key=api_key, config=config, client=client),
        SonioxRealtimeTTS(api_key=api_key, config=config, client=client),
    )


def _split_text_chunk(text: str) -> Iterator[str]:
    if not text:
        return
    for offset in range(0, len(text), SONIOX_TTS_MAX_TEXT_CHARS):
        yield text[offset : offset + SONIOX_TTS_MAX_TEXT_CHARS]


def _utterance_from(tokens: list[_FinalToken]) -> VoiceUtterance:
    text = "".join(token.text for token in tokens).strip()
    languages = Counter(token.language for token in tokens if token.language)
    language = languages.most_common(1)[0][0] if languages else ""
    return VoiceUtterance(text=text, language=language)


def _raise_event_error(event: Any, *, kind: str) -> None:
    error_code = getattr(event, "error_code", None)
    if error_code is None:
        return
    extra = getattr(event, "model_extra", None) or {}
    error_type = str(extra.get("error_type") or getattr(event, "error_type", "") or "unknown_error")
    error_message = getattr(event, "error_message", None) or f"Soniox realtime {kind} failed"
    raise SonioxVoiceError(
        f"Soniox {kind} error {error_code} ({error_type}): {error_message}",
        error_type=error_type,
        error_code=error_code,
    )


def _is_recoverable_stt_error(exc: BaseException) -> bool:
    if isinstance(exc, SonioxVoiceError):
        return exc.retryable
    for current in _exception_chain(exc):
        status_code = _status_code(current)
        if status_code in _NON_RETRYABLE_STATUS_CODES:
            return False
        name = type(current).__name__.lower()
        message = str(current).lower()
        if "validation" in name or "api key is required" in message:
            return False
        if any(error_type in message for error_type in _RETRYABLE_ERROR_TYPES):
            return True
        if isinstance(current, (ConnectionError, OSError, TimeoutError)):
            return True
        if "connectionclosed" in name:
            return True
        if "invalidstatus" in name:
            return status_code is not None and status_code >= 500
    message = str(exc).lower()
    return any(
        term in message
        for term in (
            "connection timed out",
            "failed to start realtime session",
            "failed to send audio chunk",
            "realtime session is not connected",
        )
    )


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _status_code(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    return None
