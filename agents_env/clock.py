"""Injectable wall clock so the world can outlive process restarts and tests can skip real sleeps."""

from __future__ import annotations

import asyncio
import time
from typing import Optional


class Clock:
    """Real wall clock. Sleep is real time."""

    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        delay = max(0.0, float(seconds))
        if delay == 0.0:
            await asyncio.sleep(0)
            return
        await asyncio.sleep(delay)


class VirtualClock(Clock):
    """Monotonic test clock. ``advance`` wakes waiters whose deadline has passed."""

    def __init__(self, start: float = 0.0):
        self._now = float(start)
        self._waiters: list[tuple[float, asyncio.Future[None]]] = []

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        delay = max(0.0, float(seconds))
        target = self._now + delay
        if delay == 0.0:
            await asyncio.sleep(0)
            return
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        self._waiters.append((target, fut))
        try:
            await fut
        except asyncio.CancelledError:
            if not fut.done():
                fut.cancel()
            raise
        finally:
            self._waiters = [(t, f) for t, f in self._waiters if f is not fut]

    def advance(self, seconds: float) -> None:
        self._now += max(0.0, float(seconds))
        due = [(t, f) for t, f in self._waiters if t <= self._now and not f.done()]
        for _, fut in due:
            fut.set_result(None)

    def set(self, when: float) -> None:
        if when < self._now:
            raise ValueError("virtual clock cannot go backwards")
        self.advance(when - self._now)


def default_clock(clock: Optional[Clock] = None) -> Clock:
    return clock if clock is not None else Clock()
