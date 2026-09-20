"""Soniox realtime STT/TTS adapters built on the official Python SDK."""
from __future__ import annotations

import logging
import queue
import threading
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator

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
from .partials import PartialTranscriptRelay
from .spoken_ledger import SpokenLedger
from .types import VoiceUtterance

_logger = logging.getLogger(__name__)

STT_RECONNECT_BASE_SECONDS = 0.5
STT_RECONNECT_MAX_SECONDS = 30.0
TTS_KEEPALIVE_IDLE_SECONDS = 20.0
TTS_MAX_AUDIO_DURATION_ERROR = 413
_RETRYABLE_ERROR_TYPES = frozenset(
    {
        "service_unavailable",
        "max_duration_reached",
        "internal_error",
        "request_timeout",
        "limit_exceeded",
    }
)
_RETRY_BACKOFF_SECONDS = {
    "limit_exceeded": 5.0,
    "request_timeout": 2.0,
    "internal_error": 1.0,
}
_NON_RETRYABLE_STATUS_CODES = frozenset({400, 401, 402, 403, 404, 409, 422})

_STT_RECOVERED = Callable[[], None]
_STT_RECONNECTING = Callable[[], None]


@dataclass(frozen=True)
class SonioxSTTCallbacks:
    """Optional hooks for audible STT lifecycle feedback."""

    on_reconnecting: _STT_RECONNECTING | None = None
    on_recovered: _STT_RECOVERED | None = None


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

    @property
    def retry_backoff_seconds(self) -> float | None:
        if not self.retryable or not self.error_type:
            return None
        return _RETRY_BACKOFF_SECONDS.get(self.error_type)


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
        callbacks: SonioxSTTCallbacks | None = None,
    ) -> None:
        self.api_key = api_key
        self.config = config
        self._client = client or SonioxClient(api_key=api_key)
        self._callbacks = callbacks or SonioxSTTCallbacks()
        self.partial_relay = PartialTranscriptRelay()

    def iter_utterances(
        self,
        audio_chunks: Iterable[bytes],
        *,
        pause_event: threading.Event,
        stop_event: threading.Event,
    ) -> Iterator[VoiceUtterance]:
        """Yield turns across recoverable session failures until stopped."""
        backoff = STT_RECONNECT_BASE_SECONDS
        awaiting_recovery_notice = False
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
                    if awaiting_recovery_notice:
                        awaiting_recovery_notice = False
                        recovered = self._callbacks.on_recovered
                        if recovered is not None:
                            recovered()
                    yield utterance
            except Exception as exc:
                if stop_event.is_set():
                    return
                if not _is_recoverable_stt_error(exc):
                    raise
                if isinstance(exc, SonioxVoiceError) and exc.retry_backoff_seconds:
                    backoff = max(backoff, exc.retry_backoff_seconds)
                reconnecting = self._callbacks.on_reconnecting
                if reconnecting is not None and not awaiting_recovery_notice:
                    reconnecting()
                awaiting_recovery_notice = True
                _logger.warning(
                    "Soniox STT session failed (%s); reconnecting in %.1fs",
                    exc,
                    backoff,
                )
            else:
                if stop_event.is_set():
                    return
                reconnecting = self._callbacks.on_reconnecting
                if reconnecting is not None and not awaiting_recovery_notice:
                    reconnecting()
                awaiting_recovery_notice = True
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
            draft_tokens: list[str] = []
            try:
                for event in session.receive_events():
                    if stop_event.is_set() or session_stop.is_set():
                        break
                    _raise_event_error(event, kind="STT")
                    for token in event.tokens:
                        if not token.is_final:
                            text = str(token.text or "")
                            if text and text not in {"<end>", "<fin>"}:
                                draft_tokens.append(text)
                                self.partial_relay.update("".join(draft_tokens))
                            continue
                        text = str(token.text or "")
                        if text == "<end>":
                            utterance = _utterance_from(final_tokens)
                            final_tokens = []
                            draft_tokens = []
                            self.partial_relay.clear()
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
    """Generate Soniox TTS audio using a warm multiplexed websocket connection."""

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
        self._mux_lock = threading.Lock()
        self._mux: Any | None = None
        self._active_stream: Any | None = None

    def keep_alive(self) -> None:
        with self._mux_lock:
            mux = self._mux
        if mux is not None:
            try:
                mux.keep_alive()
            except Exception:
                pass

    def cancel(self) -> None:
        self._cancel_event.set()
        with self._mux_lock:
            stream = self._active_stream
        if stream is not None:
            try:
                stream.cancel()
            except Exception:
                pass

    def close(self) -> None:
        with self._mux_lock:
            mux = self._mux
            self._mux = None
            self._active_stream = None
        if mux is not None:
            try:
                mux.close()
            except Exception:
                pass

    def synthesize_chunks(
        self,
        text_chunks: Iterable[str],
        *,
        language: str,
        stop_event: threading.Event,
        spoken_ledger: SpokenLedger | None = None,
    ) -> Iterator[bytes]:
        if stop_event.is_set() or self._cancel_event.is_set():
            return
        self._cancel_event.clear()
        pending = _TextChunkFeeder(text_chunks)
        pending.join(timeout=1.0)
        first = pending.take(timeout=0.0)
        if first is None:
            return
        pending.requeue_front(first)

        while pending.has_data() and not self._cancel_event.is_set() and not stop_event.is_set():
            stream_id = f"xagent-tts-{uuid.uuid4().hex}"
            sdk_config = RealtimeTTSConfig(
                stream_id=stream_id,
                model=SONIOX_TTS_MODEL,
                language=language,
                voice=self.config.voice,
                audio_format=SONIOX_AUDIO_FORMAT,
                sample_rate=SONIOX_TTS_SAMPLE_RATE,
                speed=self.config.speed,
                return_timestamps=self.config.return_timestamps,
            )
            send_errors: queue.Queue[BaseException] = queue.Queue()
            max_duration_hit = threading.Event()
            mux = self._ensure_mux()
            stream = mux.open_stream(config=sdk_config)
            with self._mux_lock:
                self._active_stream = stream
            sender = threading.Thread(
                target=self._send_text_loop,
                args=(stream, pending, stop_event, send_errors, max_duration_hit),
                daemon=True,
                name="xagent-soniox-tts-send",
            )
            sender.start()
            try:
                for event in stream.receive_events():
                    if not send_errors.empty():
                        raise send_errors.get()
                    if max_duration_hit.is_set():
                        break
                    if spoken_ledger is not None and event.timestamps is not None:
                        spoken_ledger.ingest_timestamps(
                            characters=list(event.timestamps.characters),
                            character_end_times_seconds=list(
                                event.timestamps.character_end_times_seconds
                            ),
                        )
                    try:
                        chunk = event.audio_bytes()
                    except ValueError as exc:
                        raise SonioxVoiceError(str(exc)) from exc
                    if chunk:
                        yield chunk
                    if event.error_code == TTS_MAX_AUDIO_DURATION_ERROR:
                        max_duration_hit.set()
                        break
                    if event.terminated:
                        break
                if not send_errors.empty():
                    raise send_errors.get()
            finally:
                sender.join(timeout=1.0)
                with self._mux_lock:
                    if self._active_stream is stream:
                        self._active_stream = None
                if max_duration_hit.is_set() and pending.has_data():
                    _logger.info("Soniox TTS stream hit max audio duration; continuing on a new stream")
                    continue
                return

    def _ensure_mux(self) -> Any:
        with self._mux_lock:
            if self._mux is None:
                mux = self._client.realtime.tts.connect_multi_stream()
                mux.__enter__()
                self._mux = mux
            return self._mux

    def _send_text_loop(
        self,
        stream: Any,
        pending: "_TextChunkFeeder",
        stop_event: threading.Event,
        send_errors: queue.Queue[BaseException],
        max_duration_hit: threading.Event,
    ) -> None:
        try:
            while pending.has_data():
                if self._cancel_event.is_set() or stop_event.is_set() or max_duration_hit.is_set():
                    stream.cancel()
                    return
                chunk = pending.take(timeout=TTS_KEEPALIVE_IDLE_SECONDS)
                if chunk is None:
                    try:
                        stream.keep_alive()
                    except Exception:
                        pass
                    continue
                for part in _split_text_chunk(chunk):
                    if self._cancel_event.is_set() or stop_event.is_set():
                        stream.cancel()
                        return
                    stream.send_text_chunk(part, text_end=False)
            if self._cancel_event.is_set() or stop_event.is_set():
                stream.cancel()
                return
            stream.finish()
        except Exception as exc:
            send_errors.put(exc)
            try:
                stream.cancel()
            except Exception:
                pass


class _TextChunkFeeder:
    """Bridge a blocking text iterator into timed reads with keepalive gaps."""

    _sentinel = object()

    def __init__(self, source: Iterable[str]) -> None:
        self._queue: queue.Queue[str | object] = queue.Queue()
        self._done = threading.Event()
        self._pushback: str | None = None
        self._thread = threading.Thread(target=self._run, args=(source,), daemon=True)
        self._thread.start()

    def _run(self, source: Iterable[str]) -> None:
        try:
            for chunk in source:
                if chunk:
                    self._queue.put(chunk)
        finally:
            self._done.set()
            self._queue.put(self._sentinel)

    def has_data(self) -> bool:
        return not (self._done.is_set() and self._queue.empty())

    def join(self, *, timeout: float = 1.0) -> None:
        self._thread.join(timeout=timeout)

    def requeue_front(self, chunk: str) -> None:
        self._pushback = chunk

    def take(self, *, timeout: float) -> str | None:
        if self._pushback is not None:
            chunk = self._pushback
            self._pushback = None
            return chunk
        if self._done.is_set() and self._queue.empty():
            return None
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is self._sentinel:
            return None
        return str(item)


def create_soniox_adapters(
    config: VoiceChannelConfig,
    *,
    stt_callbacks: SonioxSTTCallbacks | None = None,
) -> tuple[SonioxRealtimeSTT, SonioxRealtimeTTS]:
    api_key = config.resolved_api_key()
    client = SonioxClient(api_key=api_key)
    return (
        SonioxRealtimeSTT(
            api_key=api_key,
            config=config,
            client=client,
            callbacks=stt_callbacks,
        ),
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
