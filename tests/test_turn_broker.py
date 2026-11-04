"""ExecutionBroker: persistence, replay, disconnect survival, budget conversion."""
import asyncio
import unittest

from xagent.core.runtime.broker import ExecutionBroker
from xagent.core.runtime.turns import TurnStore


class _TurnIterable:
    """Fake turn generator. With ``gate`` on, each event waits for release."""

    def __init__(self, turn_id="turn-1", gate=True):
        self.turn_id = turn_id
        self.gate = gate
        self.started = asyncio.Event()
        self.advance = asyncio.Event()
        self.events = [
            {"type": "accepted", "turn_id": turn_id, "event_id": "e1"},
            {"type": "message_delta", "turn_id": turn_id, "delta": "hi"},  # live-only
            {"type": "done", "turn_id": turn_id, "event_id": "e1"},
        ]
        self.closed = False

    def __aiter__(self):
        return self

    def release(self):
        self.advance.set()

    async def __anext__(self):
        if not self.events:
            raise StopAsyncIteration
        if self.gate:
            self.started.set()
            await self.advance.wait()
            self.advance.clear()
        return self.events.pop(0)

    async def aclose(self):
        self.closed = True


class ExecutionBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_persists_structural_events_incrementally_and_replays_them(self):
        store = TurnStore()
        await store.begin(event_key="k1", turn_id="turn-1")
        iterator = _TurnIterable(gate=False)
        broker = ExecutionBroker(iterator, turn_store=store)
        sub = broker.subscribe()
        self.assertEqual((await sub.__anext__())["type"], "accepted")
        self.assertEqual((await sub.__anext__())["type"], "message_delta")
        self.assertEqual((await sub.__anext__())["type"], "done")
        # A late subscriber replays only structural events, in order.
        replay = [event async for event in broker.subscribe()]
        self.assertEqual([event["type"] for event in replay], ["accepted", "done"])
        self.assertTrue(all(event.get("replay") for event in replay))
        # Structural events were persisted with seq; live-only ones were not.
        records = await store.list_records()
        persisted = [event for event in records[0]["events"] if event["type"] != "backgrounded"]
        self.assertEqual([event["type"] for event in persisted], ["accepted", "done"])
        self.assertTrue(all("seq" in event for event in persisted))
        # Replayed events are not persisted a second time.
        self.assertEqual(len(persisted), 2)

    async def test_disconnected_subscriber_does_not_interrupt_the_turn(self):
        store = TurnStore()
        await store.begin(event_key="k1", turn_id="turn-1")
        iterator = _TurnIterable(gate=False)
        broker = ExecutionBroker(iterator, turn_store=store)
        sub = broker.subscribe()
        self.assertEqual((await sub.__anext__())["type"], "accepted")
        await sub.aclose()  # client disconnects after the first event
        await asyncio.sleep(0.05)
        self.assertFalse(iterator.closed)
        records = await store.list_records()
        self.assertEqual([event["type"] for event in records[0]["events"]], ["accepted", "done"])

    async def test_slow_subscriber_is_dropped_instead_of_blocking_execution(self):
        iterator = _TurnIterable(gate=False)
        broker = ExecutionBroker(iterator, turn_store=TurnStore())
        await broker._turn_store.begin(event_key="k1", turn_id="turn-1")
        broker.subscribe()
        full_queue = asyncio.Queue(maxsize=1)
        full_queue.put_nowait({"type": "message_delta"})
        async with broker._lock:
            broker._subscribers = [full_queue]
        await asyncio.sleep(0.05)
        self.assertNotIn(full_queue, broker._subscribers)
        records = await broker._turn_store.list_records()
        self.assertEqual([event["type"] for event in records[0]["events"]], ["accepted", "done"])

    async def test_generator_failure_emits_internal_error_and_closes_subscribers(self):
        class FailingIterator(_TurnIterable):
            async def __anext__(self):
                raise RuntimeError("boom")

        broker = ExecutionBroker(FailingIterator(), turn_store=TurnStore())
        events = [event async for event in broker.subscribe()]
        self.assertEqual(events[0]["type"], "error")
        self.assertTrue(events[0]["internal"])
        self.assertIn("boom", events[0]["error"])

    async def test_budget_conversion_emits_backgrounded_without_cancelling_the_step(self):
        state = {"now": 0.0}
        iterator = _TurnIterable()
        broker = ExecutionBroker(
            iterator, turn_store=TurnStore(), budget_seconds=1.0,
            now_provider=lambda: state["now"],
        )
        await broker._turn_store.begin(event_key="k1", turn_id="turn-1")
        sub = broker.subscribe()
        iterator.release()
        self.assertEqual((await sub.__anext__())["type"], "accepted")
        # The next step is in flight when the budget expires.
        iterator.started.clear()
        await iterator.started.wait()
        state["now"] = 5.0
        iterator.release()
        events = [await asyncio.wait_for(sub.__anext__(), 1.0)]
        while events[-1]["type"] != "done":
            iterator.release()
            events.append(await asyncio.wait_for(sub.__anext__(), 1.0))
        self.assertEqual([event["type"] for event in events], ["message_delta", "backgrounded", "done"])
        self.assertFalse(iterator.closed)

    async def test_no_budget_means_no_backgrounded_event(self):
        iterator = _TurnIterable()
        broker = ExecutionBroker(iterator, turn_store=TurnStore())
        await broker._turn_store.begin(event_key="k1", turn_id="turn-1")
        sub = broker.subscribe()
        while True:
            iterator.release()
            event = await sub.__anext__()
            if event["type"] == "done":
                break
        records = await broker._turn_store.list_records()
        self.assertFalse(any(event["type"] == "backgrounded" for event in records[0]["events"]))

    async def test_observer_runs_before_subscribers_see_the_event(self):
        store = TurnStore()
        await store.begin(event_key="k1", turn_id="turn-1")
        seen = []
        created = {"work_id": None}

        async def on_event(event):
            seen.append(event.get("type"))
            if event.get("type") == "backgrounded":
                created["work_id"] = "work-1"
                event["work_id"] = "work-1"
                event["work_status"] = "running"

        broker = ExecutionBroker(_TurnIterable(gate=False), turn_store=store,
                                 budget_seconds=0.0, on_event=on_event)
        events = [event async for event in broker.subscribe()]
        self.assertEqual([event["type"] for event in events],
                         ["accepted", "backgrounded", "message_delta", "done"])
        # The backgrounded event is only emitted once the turn identity is
        # known, and carries the observer's stamp by the time it is delivered.
        self.assertEqual(events[1]["turn_id"], "turn-1")
        self.assertEqual(events[1]["work_id"], "work-1")
        self.assertEqual(events[1]["work_status"], "running")
        self.assertEqual(created["work_id"], "work-1")
        # All structural events were persisted, including the stamped one.
        persisted = (await store.list_records())[0]["events"]
        self.assertEqual([event["type"] for event in persisted],
                         ["accepted", "backgrounded", "done"])
        self.assertEqual(persisted[1]["work_id"], "work-1")

    async def test_replayed_turns_are_never_backgrounded(self):
        seen = []

        async def on_event(event):
            seen.append(event.get("type"))

        async def replayed():
            yield {"type": "accepted", "turn_id": "turn-1", "replay": True}
            yield {"type": "done", "turn_id": "turn-1", "replay": True}

        broker = ExecutionBroker(replayed(), turn_store=TurnStore(),
                                 budget_seconds=0.0, on_event=on_event)
        events = [event async for event in broker.subscribe()]
        self.assertEqual([event["type"] for event in events], ["accepted", "done"])
        self.assertTrue(all(event.get("replay") for event in events))
        self.assertNotIn("backgrounded", seen)

    async def test_observer_failure_never_breaks_the_turn(self):
        async def on_event(event):
            raise RuntimeError("observer exploded")

        broker = ExecutionBroker(_TurnIterable(gate=False), turn_store=TurnStore(),
                                 budget_seconds=0.0, on_event=on_event)
        await broker._turn_store.begin(event_key="k1", turn_id="turn-1")
        events = [event async for event in broker.subscribe()]
        self.assertEqual(events[-1]["type"], "done")


if __name__ == "__main__":
    unittest.main()
