"""Speculative agent turns on live STT partials."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable


def normalize_transcript(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def transcripts_compatible(partial: str, final: str) -> bool:
    """True when the finalized utterance matches the prefetch transcript."""
    draft = normalize_transcript(partial)
    confirmed = normalize_transcript(final)
    return bool(draft) and draft == confirmed


ChatEventsFactory = Callable[..., AsyncIterator[dict[str, Any]]]


@dataclass
class PreemptiveGenerationConfig:
    enabled: bool = True
    min_chars: int = 8


@dataclass
class _PreemptiveSession:
    transcript: str
    queue: asyncio.Queue[dict[str, Any] | None] = field(default_factory=asyncio.Queue)
    task: asyncio.Task[None] | None = None
    failed: bool = False


class PreemptiveGenerationController:
    """Start ``chat_events`` on partial STT text; adopt when the final matches."""

    def __init__(
        self,
        config: PreemptiveGenerationConfig,
        *,
        on_abort: Callable[[], None] | None = None,
    ) -> None:
        self.config = config
        self._on_abort = on_abort
        self._lock = asyncio.Lock()
        self._session: _PreemptiveSession | None = None
        self._chat_factory: ChatEventsFactory | None = None
        self._chat_kwargs: dict[str, Any] = {}

    def configure(self, chat_factory: ChatEventsFactory, **chat_kwargs: Any) -> None:
        self._chat_factory = chat_factory
        self._chat_kwargs = dict(chat_kwargs)

    async def cancel(self) -> None:
        async with self._lock:
            await self._cancel_locked()

    async def note_partial(self, partial: str) -> None:
        if not self.config.enabled or self._chat_factory is None:
            return
        text = normalize_transcript(partial)
        if len(text) < self.config.min_chars:
            return
        async with self._lock:
            if self._session is not None and self._session.transcript == text:
                return
            await self._cancel_locked()
            session = _PreemptiveSession(transcript=text)
            session.task = asyncio.create_task(self._run_session(session, text))
            self._session = session

    async def adopt_events(self, final_transcript: str) -> AsyncIterator[dict[str, Any]] | None:
        if not self.config.enabled:
            return None
        final = normalize_transcript(final_transcript)
        async with self._lock:
            session = self._session
            if session is None or not transcripts_compatible(session.transcript, final):
                await self._cancel_locked()
                return None
            self._session = None
        assert session.task is not None
        if session.failed:
            session.task.cancel()
            await asyncio.gather(session.task, return_exceptions=True)
            return None

        async def _iter_events() -> AsyncIterator[dict[str, Any]]:
            while True:
                if session.task.done() and session.queue.empty():
                    break
                try:
                    item = await asyncio.wait_for(session.queue.get(), timeout=0.05)
                except TimeoutError:
                    if session.task.done():
                        break
                    continue
                if item is None:
                    break
                yield item
            if session.task and not session.task.done():
                await session.task
            elif session.task:
                await asyncio.gather(session.task, return_exceptions=True)

        return _iter_events()

    async def _run_session(self, session: _PreemptiveSession, transcript: str) -> None:
        assert self._chat_factory is not None
        try:
            async for event in self._chat_factory(
                user_message=transcript,
                **self._chat_kwargs,
            ):
                await session.queue.put(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            session.failed = True
        finally:
            await session.queue.put(None)

    async def _cancel_locked(self) -> None:
        session = self._session
        self._session = None
        if session is None:
            return
        if self._on_abort is not None:
            self._on_abort()
        if session.task is not None and not session.task.done():
            session.task.cancel()
            await asyncio.gather(session.task, return_exceptions=True)
        while not session.queue.empty():
            try:
                session.queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - asyncio queue API
                break
