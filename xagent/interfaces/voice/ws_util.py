"""Shared WebSocket and streaming-text helpers for voice adapters."""
from __future__ import annotations

import threading
from typing import Callable, Iterable, Iterator

# websockets defaults are 20/20; keep NAT pings but tolerate cloud/proxy latency.
WS_PING_INTERVAL_SECONDS = 20.0
WS_PING_TIMEOUT_SECONDS = 60.0
TTS_FIRST_TEXT_POLL_SECONDS = 0.25


def is_transport_error(exc: BaseException) -> bool:
    """Return True for connection drops that are safe to retry before first audio."""
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    module = getattr(type(exc), "__module__", "") or ""
    name = type(exc).__name__
    if module.startswith("websockets") and name.startswith("ConnectionClosed"):
        return True
    message = str(exc).lower()
    return "keepalive ping timeout" in message or "no close frame received" in message


class ReplayableTextSource:
    """Text source that records consumed chunks so TTS can reconnect before first audio."""

    def __init__(self, source: Iterable[str]) -> None:
        self._live_next: Callable[[float], str | None] | None = getattr(source, "next_item", None)
        if not callable(self._live_next):
            self._live_next = None
            self._live_iter: Iterator[str] | None = iter(source)
        else:
            self._live_iter = None
        self._replay: list[str] = []
        self._replay_index = 0
        self._live_exhausted = False
        self._recording = True

    @property
    def has_text(self) -> bool:
        return bool(self._replay) or self._replay_index > 0

    def next_item(self, timeout: float) -> str | None:
        if self._replay_index < len(self._replay):
            item = self._replay[self._replay_index]
            self._replay_index += 1
            return item
        if self._live_exhausted:
            raise StopIteration
        if self._live_next is not None:
            chunk = self._live_next(timeout)
            if chunk is not None and self._recording:
                self._replay.append(chunk)
                self._replay_index = len(self._replay)
            return chunk
        assert self._live_iter is not None
        try:
            chunk = next(self._live_iter)
        except StopIteration:
            self._live_exhausted = True
            raise
        if self._recording:
            self._replay.append(chunk)
            self._replay_index = len(self._replay)
        return chunk

    def rewind(self) -> None:
        self._replay_index = 0

    def commit(self) -> None:
        """Stop recording after audio has started; retry is no longer safe."""
        self._recording = False

    def __iter__(self) -> Iterator[str]:
        while True:
            try:
                item = self.next_item(timeout=10**9)
            except StopIteration:
                return
            if item is None:
                continue
            yield item


def wait_for_first_text(
    text_chunks: Iterable[str],
    *,
    stop_event: threading.Event,
    cancel_event: threading.Event,
    poll_seconds: float = TTS_FIRST_TEXT_POLL_SECONDS,
) -> ReplayableTextSource | None:
    """Block until the first non-empty text chunk, then rewind so callers can resend it.

    Returns None when the stream ends empty or stop/cancel is requested before text arrives.
    """
    source = ReplayableTextSource(text_chunks)
    while not stop_event.is_set() and not cancel_event.is_set():
        try:
            chunk = source.next_item(poll_seconds)
        except StopIteration:
            return source if source.has_text else None
        if chunk:
            source.rewind()
            return source
    return None


def reraise_send_error(exc: BaseException, *, error_cls: type[Exception]) -> None:
    """Propagate transport errors unchanged; wrap other sender failures."""
    if is_transport_error(exc):
        raise exc
    raise error_cls(str(exc)) from exc
