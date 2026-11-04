"""Small execution pool for background work.

``execute`` is injected (wired to the real turn driver in a later step), so
the runner itself stays transport-agnostic. Cancellation is cooperative: the
work record flips to ``stopping`` and the runner sets a cancel event the
driver polls between steps.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Coroutine, Optional

from .work_store import WORK_TERMINAL, WorkStore

logger = logging.getLogger(__name__)


class WorkControls:
    """Per-work control channel between the API and the executing driver."""

    def __init__(self):
        self.supplements: asyncio.Queue[tuple] = asyncio.Queue()
        self.cancel_event = asyncio.Event()

    def supplement(self, control_id: str, content: str) -> None:
        self.supplements.put_nowait((str(control_id or ""), str(content or "")))

    def request_cancel(self) -> None:
        self.cancel_event.set()

    async def next_supplement(self) -> Optional[tuple]:
        """Return ``(control_id, content)`` or None when the queue is empty."""
        try:
            return self.supplements.get_nowait()
        except asyncio.QueueEmpty:
            return None


class WorkRunner:
    def __init__(
        self,
        *,
        work_store: WorkStore,
        max_background_work: int = 2,
        execute: Optional[Callable[[dict, WorkControls], Coroutine[Any, Any, Optional[str]]]] = None,
    ) -> None:
        self.store = work_store
        self._semaphore = asyncio.Semaphore(max(1, int(max_background_work)))
        self.execute = execute
        self._controls: dict[str, WorkControls] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def controls_for(self, work_id: str) -> WorkControls:
        return self._controls.setdefault(work_id, WorkControls())

    async def submit(self, work_id: str) -> bool:
        if work_id in self._tasks and not self._tasks[work_id].done():
            return False
        try:
            record = await self.store.get(work_id)
        except KeyError:
            return False
        if record.get("status") != "queued":
            return False
        task = asyncio.create_task(self._run(work_id), name=f"xagent-work-{work_id[:8]}")
        self._tasks[work_id] = task
        task.add_done_callback(lambda done, wid=work_id: self._cleanup(wid))
        return True

    def _cleanup(self, work_id: str) -> None:
        self._controls.pop(work_id, None)

    async def supplement(self, work_id: str, control_id: str, content: str) -> bool:
        controls = self._controls.get(work_id)
        if controls is None:
            return False
        controls.supplement(control_id, content)
        return True

    async def cancel(self, work_id: str) -> bool:
        try:
            record = await self.store.get(work_id)
        except KeyError:
            return False
        if record.get("status") in WORK_TERMINAL:
            return False
        await self.store.update(work_id, status="stopping")
        controls = self._controls.get(work_id)
        if controls is not None:
            controls.request_cancel()
        return True

    async def _run(self, work_id: str) -> None:
        # The controls channel opens before the pool slot is taken, so
        # supplements sent while the work waits its turn are not lost.
        controls = self.controls_for(work_id)
        async with self._semaphore:
            record = await self.store.get(work_id)
            if record.get("status") == "stopping":
                # Cancelled while waiting for a pool slot.
                await self.store.update(work_id, status="completed", completion_reason="stopped")
                return
            if record.get("status") != "queued":
                return
            await self.store.update(work_id, status="running", started_at=time.time())
            if self.execute is None:
                return
            try:
                outcome = await self.execute(record, controls)
            except asyncio.CancelledError:
                await self.store.update(work_id, status="needs_review",
                                        needs_review_reason="execution interrupted")
                raise
            except Exception as exc:
                logger.exception("Background work failed work_id=%s", work_id)
                await self.store.update(work_id, status="failed", error=type(exc).__name__)
                return
            record = await self.store.get(work_id)
            if record.get("status") == "stopping":
                await self.store.update(work_id, status="completed", completion_reason="stopped")
            elif outcome == "failed":
                await self.store.update(work_id, status="failed", finished_at=time.time())
            elif outcome == "cancelled":
                await self.store.update(work_id, status="completed",
                                        completion_reason="cancelled", finished_at=time.time())
            else:
                await self.store.update(work_id, status="completed", finished_at=time.time())
