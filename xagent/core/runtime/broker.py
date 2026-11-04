"""One broker per turn: owns execution, persists structural events, serves subscribers.

The broker's drain task owns the turn generator, so a disconnected subscriber
never interrupts the turn. Subscribers replay persisted events first, then
receive live events. A subscriber whose queue is full is closed rather than
blocking execution.

Events are persisted incrementally through ``TurnStore.append_events``, which
uses an atomic read-modify-write, so the end-of-turn state update and the event
log never lose each other's writes.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncIterator, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Event types that are persisted and replayed after a disconnect. Live-only
# types (e.g. message_delta) are skipped to keep the stored log compact.
STRUCTURAL_EVENT_TYPES = frozenset({
    "accepted", "message_start", "message_done", "tool_call", "tool_result",
    "error", "aborted", "done", "backgrounded",
})

_END = object()


class ExecutionBroker:
    def __init__(
        self,
        turn_iterator,
        *,
        turn_store,
        budget_seconds: Optional[float] = None,
        now_provider=time.monotonic,
        on_event: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> None:
        self._iterator = turn_iterator
        self._turn_store = turn_store
        self._budget_seconds = budget_seconds
        self._now = now_provider
        self._on_event = on_event
        self._lock = asyncio.Lock()
        self._turn_id: Optional[str] = None
        self._seq = 0
        self._persisted: list[dict] = []
        self._subscribers: list[asyncio.Queue] = []
        self._closed = False
        self._backgrounded = budget_seconds is None
        self._replaying = False
        self._started_at = self._now()
        self._drain_task = asyncio.create_task(self._drain(), name="xagent-turn-broker")
        self._drain_task.add_done_callback(self._drain_done)

    def subscribe(self) -> AsyncIterator[dict]:
        """Replay persisted events, then stream live ones until the turn ends."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=128)

        async def _events():
            async with self._lock:
                replay = list(self._persisted)
                closed = self._closed
                if not closed:
                    self._subscribers.append(queue)
            for event in replay:
                yield {**event, "replay": True}
            if closed:
                return
            while True:
                event = await queue.get()
                if event is _END:
                    return
                yield event

        return _events()

    async def _drain(self) -> None:
        try:
            while True:
                # Only emit once the turn identity is known, so the event is
                # persisted with its turn_id and conversion hooks can act on it.
                # A replayed (deduplicated) turn has already finished: never
                # convert it. A turn still waiting on the inbox queue keeps its
                # identity unknown until accepted, and converts right after.
                if (not self._backgrounded and not self._replaying
                        and self._turn_id and self._budget_expired()):
                    self._backgrounded = True
                    await self._handle({"type": "backgrounded", "turn_id": self._turn_id})
                try:
                    event = await self._iterator.__anext__()
                except StopAsyncIteration:
                    break
                await self._handle(event)
        except asyncio.CancelledError:
            close = getattr(self._iterator, "aclose", None)
            if callable(close):
                await close()
            raise
        except Exception as exc:
            logger.exception("Turn broker failed turn_id=%s", self._turn_id)
            await self._handle({"type": "error", "error": str(exc), "internal": True})
        finally:
            await self._close_subscribers()

    async def _handle(self, event: dict) -> None:
        """Run the drain-side observer first, then persist and fan out.

        The observer runs before subscribers see the event, so a work record
        created by a ``backgrounded`` event is visible (its id attached to the
        event) by the time a foreground subscriber reacts to it. Observer
        failures never interrupt the turn.
        """
        replayed = "seq" in event or event.get("replay")
        if not replayed and self._on_event is not None:
            try:
                await self._on_event(event)
            except Exception:
                logger.exception("Turn observer failed turn_id=%s type=%s",
                                 self._turn_id, event.get("type"))
        await self._emit(event)

    def _budget_expired(self) -> bool:
        return (self._now() - self._started_at) >= self._budget_seconds

    async def _emit(self, event: dict) -> None:
        replayed = "seq" in event or event.get("replay")
        if not replayed:
            self._seq += 1
            event = {**event, "seq": self._seq}
        turn_id = event.get("turn_id")
        if turn_id:
            self._turn_id = self._turn_id or str(turn_id)
            if replayed:
                self._replaying = True
        structural = event.get("type") in STRUCTURAL_EVENT_TYPES
        persist = structural and not replayed and self._turn_id
        async with self._lock:
            if persist:
                self._persisted.append(event)
            for queue in list(self._subscribers):
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    self._subscribers.remove(queue)
        if persist:
            try:
                await self._turn_store.append_events(self._turn_id, [event])
            except Exception:
                logger.exception("Could not persist turn event turn_id=%s type=%s",
                                 self._turn_id, event.get("type"))

    async def _close_subscribers(self) -> None:
        async with self._lock:
            self._closed = True
            queues = list(self._subscribers)
            self._subscribers.clear()
        for queue in queues:
            try:
                queue.put_nowait(_END)
            except asyncio.QueueFull:
                pass

    def _drain_done(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.error("Turn broker drain failed", exc_info=task.exception())
