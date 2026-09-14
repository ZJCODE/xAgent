"""Tests for the waking-turn cancellation primitive."""

import asyncio
import unittest

from xagent.core.turn import (
    TurnAborted,
    TurnCancel,
    bind_turn_cancel,
    current_turn_cancel,
    iter_until_cancelled,
    reset_turn_cancel,
    wait_first,
)


class TurnCancelTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_first_returns_ok_when_work_finishes(self):
        async def done():
            return "value"

        work = asyncio.create_task(done())
        status = await wait_first(work, timeout=1)
        self.assertEqual(status, "ok")
        self.assertEqual(work.result(), "value")

    async def test_wait_first_returns_timeout_without_cancelling_work(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked():
            started.set()
            await release.wait()
            return "later"

        work = asyncio.create_task(blocked())
        await started.wait()
        status = await wait_first(work, timeout=0.05)
        self.assertEqual(status, "timeout")
        self.assertFalse(work.done())
        release.set()
        self.assertEqual(await work, "later")

    async def test_wait_first_returns_aborted_from_bound_cancel(self):
        cancel = TurnCancel()
        token = bind_turn_cancel(cancel)
        started = asyncio.Event()

        async def blocked():
            started.set()
            await asyncio.Event().wait()

        try:
            work = asyncio.create_task(blocked())
            await started.wait()
            cancel.request()
            status = await wait_first(work, timeout=1)
            self.assertEqual(status, "aborted")
            self.assertFalse(work.done())
            work.cancel()
            try:
                await work
            except asyncio.CancelledError:
                pass
        finally:
            reset_turn_cancel(token)

    async def test_iter_until_cancelled_raises_and_stops_the_stream(self):
        cancel = TurnCancel()
        token = bind_turn_cancel(cancel)
        closed = asyncio.Event()
        release = asyncio.Event()

        async def stream():
            try:
                yield "Hel"
                await release.wait()
                yield "lo"
            finally:
                closed.set()

        try:
            agen = stream()
            aiter = iter_until_cancelled(agen).__aiter__()
            self.assertEqual(await aiter.__anext__(), "Hel")
            cancel.request()
            with self.assertRaises(TurnAborted):
                await asyncio.wait_for(aiter.__anext__(), timeout=1)
            await asyncio.wait_for(closed.wait(), timeout=1)
            self.assertFalse(release.is_set())
        finally:
            reset_turn_cancel(token)

    def test_bind_exposes_current_turn_cancel(self):
        cancel = TurnCancel()
        self.assertIsNone(current_turn_cancel())
        token = bind_turn_cancel(cancel)
        try:
            self.assertIs(current_turn_cancel(), cancel)
        finally:
            reset_turn_cancel(token)
        self.assertIsNone(current_turn_cancel())
