"""Unit tests for Trigger bus, Fast Path, and async Worker start/stop."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from xagent.core.runtime.async_jobs import AsyncJobRunner, JobStatus
from xagent.core.runtime.router import AgentRouter
from xagent.core.runtime.subconscious import ContactEntry, SubconsciousLoop
from xagent.core.runtime.triggers import (
    FastPathDecision,
    FastPathFilter,
    TriggerBus,
    TriggerKind,
    make_trigger,
)


class TriggerModelTests(unittest.TestCase):
    def test_make_trigger_normalizes_fields(self):
        trigger = make_trigger(
            source=" weixin ",
            kind="direct_request",
            actor_id="  u1 ",
            channel=" WeiXin ",
            thread_id=" room-a ",
            payload_summary="hello",
            payload={"content": "hello"},
        )
        self.assertEqual(trigger.source, "weixin")
        self.assertEqual(trigger.kind, TriggerKind.DIRECT_REQUEST)
        self.assertEqual(trigger.actor_id, "u1")
        self.assertEqual(trigger.channel, "weixin")
        self.assertEqual(trigger.thread_id, "room-a")
        self.assertTrue(trigger.id)
        self.assertIn("kind", trigger.to_dict())


class TriggerBusTests(unittest.TestCase):
    def test_enqueue_subscribe_drain_batch(self):
        bus = TriggerBus(max_pending=10)
        seen = []
        unsub = bus.subscribe(lambda trigger: seen.append(trigger.id))
        first = make_trigger(source="api", kind=TriggerKind.DIRECT_REQUEST, actor_id="a")
        second = make_trigger(source="voice", kind=TriggerKind.OBSERVATION, actor_id="b")
        bus.enqueue(first)
        bus.enqueue(second)
        self.assertEqual(bus.pending_count, 2)
        self.assertEqual(seen, [first.id, second.id])
        batch = bus.drain(max_items=1)
        self.assertEqual([item.id for item in batch], [first.id])
        self.assertEqual(bus.pending_count, 1)
        rest = bus.drain()
        self.assertEqual([item.id for item in rest], [second.id])
        self.assertEqual(bus.pending_count, 0)
        unsub()
        bus.enqueue(make_trigger(source="api", kind=TriggerKind.TIMER))
        self.assertEqual(seen, [first.id, second.id])

    def test_capacity_drops_oldest(self):
        bus = TriggerBus(max_pending=2)
        a = make_trigger(source="a", kind=TriggerKind.TIMER)
        b = make_trigger(source="b", kind=TriggerKind.TIMER)
        c = make_trigger(source="c", kind=TriggerKind.TIMER)
        bus.enqueue(a)
        bus.enqueue(b)
        bus.enqueue(c)
        pending = bus.peek()
        self.assertEqual([item.source for item in pending], ["b", "c"])
        self.assertEqual(bus.dropped_count, 1)


class FastPathFilterTests(unittest.TestCase):
    def test_direct_request_requires_actor(self):
        filt = FastPathFilter()
        trigger = make_trigger(source="api", kind=TriggerKind.DIRECT_REQUEST)
        result = filt.evaluate(trigger)
        self.assertEqual(result.decision, FastPathDecision.DROP)
        self.assertEqual(result.rule, "require_actor_for_direct")

    def test_direct_request_allow_with_actor(self):
        filt = FastPathFilter()
        trigger = make_trigger(source="api", kind=TriggerKind.DIRECT_REQUEST, actor_id="u1", channel="api")
        result = filt.evaluate(trigger)
        self.assertEqual(result.decision, FastPathDecision.ALLOW)

    def test_channel_allowlist_drop(self):
        filt = FastPathFilter(allowed_channels={"api", "weixin"})
        trigger = make_trigger(
            source="voice",
            kind=TriggerKind.DIRECT_REQUEST,
            actor_id="u1",
            channel="telegram",
        )
        result = filt.evaluate(trigger)
        self.assertEqual(result.decision, FastPathDecision.DROP)
        self.assertEqual(result.rule, "channel_allowlist")

    def test_device_not_paired_drop(self):
        filt = FastPathFilter(paired_device_ids={"dev-1"})
        trigger = make_trigger(
            source="devices",
            kind=TriggerKind.DEVICE,
            actor_id="dev-9",
            payload={"device_id": "dev-9"},
        )
        result = filt.evaluate(trigger)
        self.assertEqual(result.decision, FastPathDecision.DROP)
        self.assertEqual(result.rule, "device_not_paired")

    def test_device_paired_defers_or_allows_path(self):
        filt = FastPathFilter(paired_device_ids=lambda: {"dev-1"})
        trigger = make_trigger(
            source="devices",
            kind=TriggerKind.DEVICE,
            payload={"device_id": "dev-1"},
            payload_summary="button",
        )
        result = filt.evaluate(trigger)
        self.assertEqual(result.decision, FastPathDecision.DEFER_TO_SLOW_PATH)

    def test_subconscious_candidate_empty_recipient_drop(self):
        log: list[dict] = []
        filt = FastPathFilter(decision_log=log)
        trigger = make_trigger(
            source="subconscious",
            kind=TriggerKind.SUBCONSCIOUS_CANDIDATE,
            payload={"recipient_hint": ""},
        )
        result = filt.evaluate(trigger)
        self.assertEqual(result.decision, FastPathDecision.DROP)
        self.assertEqual(result.rule, "empty_recipient")
        self.assertEqual(log[-1]["decision"], "drop")

    def test_quiet_hours_drop_candidate(self):
        filt = FastPathFilter(
            quiet_hours=(22, 7),
            clock=lambda: datetime(2026, 11, 3, 23, 0, 0),
        )
        trigger = make_trigger(
            source="subconscious",
            kind=TriggerKind.SUBCONSCIOUS_CANDIDATE,
            payload={"recipient_hint": "alice"},
        )
        result = filt.evaluate(trigger)
        self.assertEqual(result.decision, FastPathDecision.DROP)
        self.assertEqual(result.rule, "quiet_hours")

    def test_timer_defers_to_slow_path(self):
        filt = FastPathFilter()
        trigger = make_trigger(source="subconscious", kind=TriggerKind.TIMER)
        result = filt.evaluate(trigger)
        self.assertEqual(result.decision, FastPathDecision.DEFER_TO_SLOW_PATH)


class AsyncJobRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_complete_enqueues_job_result_trigger(self):
        bus = TriggerBus()
        runner = AsyncJobRunner(bus)

        async def work():
            await asyncio.sleep(0.01)
            return {"summary": "done-ok"}

        handle = runner.start("demo", work, channel="api", actor_id="u1", thread_id="t1")
        self.assertEqual(handle.status, JobStatus.STARTED)
        self.assertTrue(handle.job_id)
        finished = await runner.wait(handle.job_id, timeout=2.0)
        self.assertEqual(finished.status, JobStatus.COMPLETED)
        self.assertEqual(finished.result_summary, "done-ok")
        batch = bus.drain()
        self.assertEqual(len(batch), 1)
        trigger = batch[0]
        self.assertEqual(trigger.kind, TriggerKind.JOB_RESULT)
        self.assertEqual(trigger.payload["job_id"], handle.job_id)
        self.assertEqual(trigger.payload["status"], "completed")
        self.assertFalse(trigger.metadata.get("worker_speaks_to_user"))
        self.assertTrue(trigger.metadata.get("route_via_main_loop"))

    async def test_stop_cancels_running_job(self):
        bus = TriggerBus()
        runner = AsyncJobRunner(bus)
        started = asyncio.Event()

        async def work():
            started.set()
            await asyncio.sleep(60)
            return "should-not-finish"

        handle = runner.start("slow", work, actor_id="u1")
        await asyncio.wait_for(started.wait(), timeout=1.0)
        self.assertTrue(runner.stop(handle.job_id))
        finished = await runner.wait(handle.job_id, timeout=2.0)
        self.assertEqual(finished.status, JobStatus.STOPPED)
        batch = bus.drain()
        self.assertEqual(batch[0].payload["status"], "stopped")


class AgentRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_router_admits_and_routes_job_results(self):
        handled = []

        async def on_job_result(trigger):
            handled.append(trigger.payload["job_id"])

        router = AgentRouter(job_result_handler=on_job_result)
        admission = router.admit_direct_request(
            content="hi",
            actor_id="u1",
            channel="api",
        )
        self.assertTrue(admission.accepted)
        self.assertEqual(admission.fast_path.decision, FastPathDecision.ALLOW)

        async def work():
            return "ok"

        handle = router.start_job("unit", work, actor_id="u1", channel="api")
        await router.jobs.wait(handle.job_id, timeout=2.0)
        leftovers = await router.drain_and_route()
        # direct request remains for main loop; job result consumed by handler
        self.assertEqual([item.kind for item in leftovers], [TriggerKind.DIRECT_REQUEST])
        self.assertEqual(handled, [handle.job_id])

    def test_router_drop_is_logged(self):
        router = AgentRouter()
        admission = router.admit(
            make_trigger(source="api", kind=TriggerKind.DIRECT_REQUEST),
        )
        self.assertFalse(admission.accepted)
        state = router.state()
        self.assertTrue(any(row.get("rule") == "require_actor_for_direct" for row in state.recent_drops))


class SubconsciousFastPathIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_route_drops_empty_recipient_via_fast_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            bus = TriggerBus()
            loop = SubconsciousLoop(
                agent=MagicMock(),
                workspace=workspace,
                delivery_sink=AsyncMock(),
                deliverable_channels={"api"},
                trigger_bus=bus,
            )
            delivered = await loop._route_subconscious_thought(
                "hello there",
                "internal note",
                recipient_hint="",
                expected_cursor=None,
            )
            self.assertFalse(delivered)
            loop._delivery_sink.assert_not_awaited()
            drained = bus.drain()
            self.assertTrue(any(item.kind is TriggerKind.SUBCONSCIOUS_CANDIDATE for item in drained))
            self.assertTrue(
                any(
                    row.get("rule") == "empty_recipient"
                    for row in loop._fast_path_log
                )
            )

    async def test_route_delivers_when_fast_path_allows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            contacts_file = workspace / "contacts.json"
            from xagent.core.runtime.subconscious import save_contacts

            save_contacts(
                contacts_file,
                [
                    ContactEntry(
                        channel="api",
                        user_id="web_user",
                        target={"user_id": "web_user", "sender_name": "Alice"},
                        last_seen="2026-11-03 12:00:00",
                        interaction_count=2,
                    )
                ],
            )
            sink = AsyncMock()
            bus = TriggerBus()
            agent = MagicMock()
            agent.inbox = MagicMock(busy=False, pending_count=0)
            agent.message_storage = MagicMock()
            agent.message_storage.get_latest_message_cursor = MagicMock(return_value=1)
            loop = SubconsciousLoop(
                agent=agent,
                workspace=workspace,
                delivery_sink=sink,
                deliverable_channels={"api"},
                trigger_bus=bus,
            )
            delivered = await loop._route_subconscious_thought(
                "hello Alice",
                "I should say hi",
                recipient_hint="Alice",
                expected_cursor=1,
            )
            self.assertTrue(delivered)
            sink.assert_awaited()
            self.assertTrue(
                any(row.get("decision") == "allow" for row in loop._fast_path_log)
            )


if __name__ == "__main__":
    unittest.main()
