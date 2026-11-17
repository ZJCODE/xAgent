"""Autonomous work shares normal tools, receipts and foreground ownership."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from tests import test_agent_inbox as fixtures
from tests.test_agent_chat_flow import CapturingModelClient, InMemoryMessageStorage, FakeToolCall, FakeToolExecutor
from xagent.core.config import ReplyType
from xagent.core.inbox import AgentInbox, InboxCapacityError
from xagent.core.runtime.subconscious import SubconsciousLoop, upsert_contact
from xagent.core.runtime.autonomous import AutonomousWake
from xagent.core.handlers.message import MessageHandler


def result(outcome="idle", **kwargs):
    return json.dumps({"outcome": outcome, "summary": "nothing new", "next_step": None,
                       "blocked_on": None, "note": None, "message": None, **kwargs})


class AutonomousTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def build(self, responses, executor=None):
        model = CapturingModelClient(responses)
        storage = InMemoryMessageStorage()
        agent = fixtures.AgentInboxTests()._build_agent(storage, model, tool_executor=executor)
        agent.subconscious_mode = "act"
        from unittest.mock import AsyncMock
        agent.record_subconscious_thought = AsyncMock()
        loop = SubconsciousLoop(agent, workspace=self.root, interval_seconds=1)
        wake = AutonomousWake(loop)
        wake.state["next_run_at"] = 0
        loop._autonomous = wake
        return agent, loop, wake, model, storage

    async def test_normal_engine_executes_tools_and_records_internal_origin(self):
        executor = FakeToolExecutor()
        agent, loop, wake, model, storage = self.build([
            (ReplyType.TOOL_CALL, [FakeToolCall()]),
            (ReplyType.SIMPLE_REPLY, result("completed", summary="verified task"))], executor)
        await loop.maybe_think()
        self.assertEqual(len(executor.seen_input_messages), 1)
        self.assertEqual(wake.state["last_exit"], "completed")
        record = (await agent.turn_store.list_records())[0]
        self.assertEqual(record["source"], "autonomous")
        self.assertEqual(record["autonomous_tool_results"], 1)
        self.assertEqual(storage.messages, [])
        self.assertIn("available_recipients", record["internal_input"])
        self.assertNotIn("previous_wake", str(model.calls[0]))
        self.assertIsNone(agent.memory_handler.experience_messages)
        self.assertNotIn("No tools", str(model.instructions_calls))

    async def test_idle_backoff_survives_restart_and_user_request_resets_it(self):
        agent, loop, wake, _, _ = self.build([(ReplyType.SIMPLE_REPLY, result())])
        await loop.maybe_think()
        self.assertEqual(wake.state["quiet_count"], 1)
        restored = AutonomousWake(loop)
        self.assertEqual(restored.state["quiet_count"], 1)
        ticket = agent.inbox.reserve_turn(user_id="alice")
        agent.inbox.discard(ticket)
        await restored.tick()
        self.assertEqual(restored.state["quiet_count"], 0)

    async def test_off_mode_does_not_wake(self):
        agent, loop, _, model, _ = self.build([])
        agent.subconscious_mode = "off"
        await loop.maybe_think()
        self.assertEqual(model.calls, [])

    async def test_invalid_final_does_not_reexecute_tool_to_repair_output(self):
        executor = FakeToolExecutor()
        agent, loop, wake, _, _ = self.build([(ReplyType.TOOL_CALL, [FakeToolCall()]),
            (ReplyType.SIMPLE_REPLY, "bad JSON")], executor)
        await loop.maybe_think()
        self.assertEqual(len(executor.seen_input_messages), 1)
        restored = AutonomousWake(loop)
        self.assertEqual(restored.state["last_exit"], "failed")
        receipt = (await agent.turn_store.list_records())[0]
        self.assertTrue(receipt["internal_tool_events"])
        self.assertNotIn("tool_events", restored.state)
        self.assertIn("active_wake", restored.state)

    async def test_user_preempts_and_waits_for_tool_cleanup(self):
        started, cleaned = asyncio.Event(), asyncio.Event()
        class BlockingExecutor:
            async def handle_tool_calls(self, *args, **kwargs):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0.01)
                    cleaned.set()
        agent, loop, wake, _, _ = self.build([(ReplyType.TOOL_CALL, [FakeToolCall()]),
            (ReplyType.SIMPLE_REPLY, "hello")], BlockingExecutor())
        background = asyncio.create_task(loop.maybe_think())
        await asyncio.wait_for(started.wait(), 1)
        foreground = [event async for event in agent.chat_events("hello", user_id="alice", channel="api")]
        await asyncio.wait_for(background, 1)
        self.assertTrue(cleaned.is_set())
        self.assertTrue(any(event.get("content") == "hello" for event in foreground))
        records = await agent.turn_store.list_records()
        automatic = next(r for r in records if r.get("source") == "autonomous")
        self.assertEqual(automatic["state"], "interrupted")
        self.assertTrue(automatic["needs_review"])
        self.assertEqual(wake.state["last_exit"], "interrupted")

    async def test_repeated_tool_arguments_are_bounded(self):
        executor = FakeToolExecutor()
        _, loop, wake, _, _ = self.build([
            (ReplyType.TOOL_CALL, [FakeToolCall()]) for _ in range(3)], executor)
        await loop.maybe_think()
        self.assertEqual(len(executor.seen_input_messages), 3)
        self.assertEqual(wake.state["last_exit"], "failed")

    async def test_ten_idle_wakes_do_not_append_reworded_diary_notes(self):
        agent, loop, wake, _, _ = self.build([(ReplyType.SIMPLE_REPLY,
            result("idle", summary=f"still pending {n}", note=f"I have not done it {n}")) for n in range(10)])
        from unittest.mock import AsyncMock
        agent.record_subconscious_thought = AsyncMock()
        for _ in range(10):
            wake.state["next_run_at"] = 0
            await loop.maybe_think()
        agent.record_subconscious_thought.assert_not_awaited()
        self.assertEqual(wake.state["quiet_count"], 10)

    async def test_notification_enqueue_recovers_without_reexecuting_work(self):
        _, loop, wake, model, _ = self.build([(ReplyType.SIMPLE_REPLY,
            result("completed", summary="done", message={"recipient": "weixin:alice", "text": "done"}))])
        loop.set_deliverable_channels(["weixin"])
        upsert_contact(loop.contacts_file, channel="weixin", user_id="alice", target={"user_id": "alice"})
        loop._delivery_retries = 0
        timestamps = []
        async def sink(delivery):
            timestamps.append(delivery.created_at)
            if len(timestamps) == 1:
                raise RuntimeError("interrupted enqueue acknowledgement")
            return {"queued": True}
        loop._delivery_sink = sink
        await loop.maybe_think()
        restored = AutonomousWake(loop)
        await restored.tick()
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(timestamps[0], timestamps[1])
        self.assertNotIn("pending_notification", restored.state)

    async def test_different_arguments_are_not_treated_as_repeated_tools(self):
        executor = FakeToolExecutor()
        _, loop, wake, _, _ = self.build([
            *[(ReplyType.TOOL_CALL, [FakeToolCall(arguments=json.dumps({"page": page}))]) for page in range(3)],
            (ReplyType.SIMPLE_REPLY, result("completed", summary="checked pages"))], executor)
        await loop.maybe_think()
        self.assertEqual(len(executor.seen_input_messages), 3)
        self.assertEqual(wake.state["last_exit"], "completed")

    async def test_progress_continues_through_real_diary_without_previous_wake(self):
        from xagent.core.agent import Agent
        agent = Agent(client=object(), workspace=str(self.root), subconscious_mode="act")
        model = CapturingModelClient([
            (ReplyType.TOOL_CALL, [FakeToolCall()]),
            (ReplyType.SIMPLE_REPLY, result("progressed", summary="Morning delivery configuration has been merged and read back.",
                next_step="Verify the old schedules are paused.")),
            (ReplyType.SIMPLE_REPLY, result("idle"))])
        agent.model_client = model
        agent.tool_executor = FakeToolExecutor()
        loop = SubconsciousLoop(agent, workspace=self.root, interval_seconds=60)
        wake = AutonomousWake(loop)
        loop._autonomous = wake
        wake.state["next_run_at"] = 0
        await loop.maybe_think()
        first_diary = await agent.markdown_memory.read_recent_dailies(days=1)
        self.assertIn("configuration has been merged", str(first_diary))
        self.assertIn("Verify the old schedules", str(first_diary))
        # Rebuild the controller to ensure continuity comes from persisted diary.
        restored = AutonomousWake(loop)
        loop._autonomous = restored
        restored.state["next_run_at"] = 0
        await loop.maybe_think()
        memory = next(m for m in model.calls[-1] if m.get("name") == "recent_memory")
        self.assertIn("configuration has been merged", memory["content"])
        self.assertIn("Verify the old schedules", memory["content"])
        self.assertNotIn("previous_wake", str(model.calls[-1]))
        self.assertNotIn("quiet_count", str(model.calls[-1]))
        self.assertEqual(await agent.markdown_memory.read_recent_dailies(days=1), first_diary)
        self.assertEqual(await agent.message_storage.get_message_count(), 0)

    async def test_new_blocker_is_recorded_once_without_repetition(self):
        agent, loop, wake, _, _ = self.build([
            (ReplyType.SIMPLE_REPLY, result("blocked", summary="The server rejected the request.", blocked_on="Needs a fresh context.")),
            (ReplyType.SIMPLE_REPLY, result("blocked", summary="Still waiting for the server.", blocked_on="Needs a fresh context."))])
        for _ in range(2):
            wake.state["next_run_at"] = 0
            await loop.maybe_think()
        agent.record_subconscious_thought.assert_awaited_once_with("The server rejected the request.\nNeeds a fresh context.")

    async def test_legacy_work_snapshot_is_discarded_from_scheduler_state(self):
        _, loop, wake, _, _ = self.build([])
        wake.state.update(last_result={"summary": "stale work snapshot"},
                          tool_events=[{"name": "old_tool"}], recent_notes=["old note"])
        wake.save()
        restored = AutonomousWake(loop)
        self.assertNotIn("last_result", restored.state)
        self.assertNotIn("tool_events", restored.state)
        self.assertNotIn("recent_notes", restored.state)
        restored.save()
        self.assertNotIn("stale work snapshot", restored.path.read_text())

    async def test_background_never_queues_behind_foreground(self):
        inbox = AgentInbox()
        ticket = inbox.reserve_turn(user_id="alice")
        with self.assertRaises(InboxCapacityError):
            inbox.reserve_turn(background=True)
        inbox.discard(ticket)
