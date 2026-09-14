"""Cancellation for one waking turn.

A waking turn is one act of the agent. Stop means this act is over.
Every in-flight wait — the model stream, a shell process, later HTTP tools —
races the same signal. How each resource actually stops is local; the signal
is not.

Inbox remains a delivery seam. This module is the turn's stop primitive.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar, Token
from typing import Any, AsyncIterator, Optional, TypeVar

T = TypeVar("T")

_CURRENT: ContextVar[Optional["TurnCancel"]] = ContextVar(
    "xagent_turn_cancel",
    default=None,
)


class TurnAborted(Exception):
    """The current waking turn was cancelled."""


class TurnCancel:
    """One-shot cancellation token for a claimed waking turn."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def event(self) -> asyncio.Event:
        return self._event

    def request(self) -> None:
        self._event.set()

    def requested(self) -> bool:
        return self._event.is_set()

    def reset(self) -> None:
        self._event.clear()


def current_turn_cancel() -> Optional[TurnCancel]:
    """Cancel token for the in-flight waking turn, if any."""
    return _CURRENT.get()


def bind_turn_cancel(cancel: Optional[TurnCancel]) -> Token:
    """Bind this turn's cancel token for model calls and tools."""
    return _CURRENT.set(cancel)


def reset_turn_cancel(token: Token) -> None:
    """Restore the previous turn cancel binding."""
    _CURRENT.reset(token)


async def wait_first(
    work: asyncio.Task,
    *,
    cancel: Optional[TurnCancel] = None,
    timeout: Optional[float] = None,
) -> str:
    """Wait until ``work`` finishes, the turn is cancelled, or ``timeout``.

    Returns ``ok``, ``aborted``, or ``timeout``. Never cancels ``work``:
    the caller owns how that resource stops (aclose a stream, kill a
    process group, then drain).
    """
    if cancel is None:
        cancel = current_turn_cancel()
    if cancel is not None and cancel.requested():
        return "aborted"

    waiters: set[asyncio.Task] = {work}
    abort_task: Optional[asyncio.Task] = None
    if cancel is not None:
        abort_task = asyncio.create_task(cancel.event.wait())
        waiters.add(abort_task)

    done, pending = await asyncio.wait(
        waiters,
        timeout=timeout,
        return_when=asyncio.FIRST_COMPLETED,
    )
    await _cancel_task(abort_task if abort_task is not None and abort_task in pending else None)

    if cancel is not None and cancel.requested():
        return "aborted"
    if work in done and not work.cancelled():
        return "ok"
    return "timeout"


async def iter_until_cancelled(
    agen: AsyncIterator[T],
    *,
    cancel: Optional[TurnCancel] = None,
) -> AsyncIterator[T]:
    """Yield from ``agen`` until the turn is cancelled, then aclose it.

    Raises ``TurnAborted`` instead of inventing a model event. Abort is
    turn control, not something the provider said.
    """
    if cancel is None:
        cancel = current_turn_cancel()
    if cancel is None:
        async for item in agen:
            yield item
        return

    aiter = agen.__aiter__()
    end = object()

    async def anext_or_end() -> Any:
        try:
            return await aiter.__anext__()
        except StopAsyncIteration:
            return end

    try:
        while True:
            if cancel.requested():
                raise TurnAborted()
            next_task = asyncio.create_task(anext_or_end())
            status = await wait_first(next_task, cancel=cancel)
            if status == "aborted":
                await _cancel_task(next_task)
                raise TurnAborted()
            item = next_task.result()
            if item is end:
                return
            yield item
    finally:
        await _aclose_async_generator(aiter)


async def _cancel_task(task: Optional[asyncio.Task]) -> None:
    if task is None:
        return
    if not task.done():
        task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


async def _aclose_async_generator(aiter: Any) -> None:
    aclose = getattr(aiter, "aclose", None)
    if not callable(aclose):
        return
    try:
        await aclose()
    except (asyncio.CancelledError, Exception):
        pass
