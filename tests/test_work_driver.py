"""The host's work driver: runs queued work as serialized work_turn turns,
steers supplements, and cooperates with cancellation."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from xagent.core.runtime.host import RuntimeHost
from xagent.core.runtime.turns import TurnStore
from xagent.core.runtime.work_runner import WorkControls, WorkRunner
from xagent.core.runtime.work_store import WorkStore
from xagent.core.tooling.manager import ToolManager


def _scripted(events):
    class ScriptedAgent:
        def __init__(self):
            self._events = list(events)
            self.chat_calls = []
            self.steers = []
            self.aborts = []
            self.steer_result = True
            self.abort_result = True

        async def chat_events(self, *, user_message, user_id, channel, room_name,
                              inbox_kind, work_id=None, **kwargs):
            self.chat_calls.append(dict(
                user_message=user_message, user_id=user_id, channel=channel,
                room_name=room_name, inbox_kind=inbox_kind, work_id=work_id,
            ))
            for event in self._events:
                yield event

        async def steer(self, turn_id, content, *, channel=None, user_id=None, room_name=None):
            self.steers.append((turn_id, content, channel, user_id, room_name))
            return self.steer_result

        def abort(self, turn_id=None, *, channel=None):
            self.aborts.append((turn_id, channel))
            return self.abort_result

    return ScriptedAgent()


def _host_with(agent):
    host = RuntimeHost(Path(tempfile.mkdtemp()))
    host.agent = agent
    host.work_store = WorkStore()
    return host


async def _record(work_store):
    return await work_store.create(channel="api", user_id="alice", room_name="",
                                   source="delegated", goal="do the thing")


class WorkDriverTests(unittest.IsolatedAsyncioTestCase):
    async def test_runs_the_goal_as_a_work_turn_and_records_the_turn(self):
        agent = _scripted([
            {"type": "accepted", "turn_id": "turn-1"},
            {"type": "message_done", "phase": "final", "content": "done"},
            {"type": "done", "turn_id": "turn-1"},
        ])
        host = _host_with(agent)
        record = await _record(host.work_store)
        outcome = await host._execute_work(record, WorkControls())
        self.assertIsNone(outcome)
        self.assertEqual(agent.chat_calls[0]["user_message"], "do the thing")
        self.assertEqual(agent.chat_calls[0]["inbox_kind"], "work_turn")
        self.assertEqual(agent.chat_calls[0]["work_id"], record["id"])
        self.assertEqual((await host.work_store.get(record["id"]))["turn_ids"], ["turn-1"])

    async def test_error_event_fails_the_work(self):
        agent = _scripted([
            {"type": "accepted", "turn_id": "turn-1"},
            {"type": "error", "error": "model exploded"},
            {"type": "done", "turn_id": "turn-1"},
        ])
        host = _host_with(agent)
        record = await _record(host.work_store)
        self.assertEqual(await host._execute_work(record, WorkControls()), "failed")

    async def test_supplements_steer_into_the_turn_and_are_marked_incorporated(self):
        agent = _scripted([
            {"type": "accepted", "turn_id": "turn-1"},
            {"type": "message_start"},
            {"type": "done", "turn_id": "turn-1"},
        ])
        host = _host_with(agent)
        record = await _record(host.work_store)
        controls = WorkControls()
        control_id = await host.work_store.append_control(record["id"], "also fix the docs")
        controls.supplement(control_id, "also fix the docs")
        await host._execute_work(record, controls)
        self.assertEqual(agent.steers, [("turn-1", "also fix the docs", "api", "alice", "")])
        stored = await host.work_store.get(record["id"])
        self.assertEqual(stored["controls"][0]["state"], "incorporated")

    async def test_refused_steer_leaves_the_control_received(self):
        agent = _scripted([
            {"type": "accepted", "turn_id": "turn-1"},
            {"type": "message_start"},
            {"type": "done", "turn_id": "turn-1"},
        ])
        agent.steer_result = False
        host = _host_with(agent)
        record = await _record(host.work_store)
        controls = WorkControls()
        control_id = await host.work_store.append_control(record["id"], "more")
        controls.supplement(control_id, "more")
        await host._execute_work(record, controls)
        stored = await host.work_store.get(record["id"])
        self.assertEqual(stored["controls"][0]["state"], "received")

    async def test_cancel_aborts_the_running_turn_and_reports_cancelled(self):
        gate = asyncio.Event()
        entered_gate = asyncio.Event()
        agent = _scripted([
            {"type": "accepted", "turn_id": "turn-1"},
            {"type": "aborted", "turn_id": "turn-1"},
            {"type": "done", "turn_id": "turn-1"},
        ])
        original = agent.chat_events

        async def gated_chat_events(**kwargs):
            yielded_first = False
            async for event in original(**kwargs):
                if not yielded_first:
                    yielded_first = True
                else:
                    entered_gate.set()
                    await gate.wait()
                yield event

        agent.chat_events = gated_chat_events
        host = _host_with(agent)
        record = await _record(host.work_store)
        controls = WorkControls()
        task = asyncio.create_task(host._execute_work(record, controls))
        await asyncio.wait_for(entered_gate.wait(), 2.0)
        controls.request_cancel()
        gate.set()
        self.assertEqual(await task, "cancelled")
        self.assertEqual(agent.aborts, [("turn-1", "api")])

    async def test_cancel_before_start_never_opens_a_turn(self):
        agent = _scripted([])
        host = _host_with(agent)
        record = await _record(host.work_store)
        controls = WorkControls()
        controls.request_cancel()
        self.assertIsNone(await host._execute_work(record, controls))
        self.assertEqual(agent.chat_calls, [])


class WorkRuntimeWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_ensure_work_runtime_wires_execute_agent_and_tools(self):
        class AgentWithTools:
            def __init__(self):
                self.turn_store = TurnStore()
                self.tool_manager = ToolManager()

        host = RuntimeHost(Path(tempfile.mkdtemp()))
        host.agent = AgentWithTools()
        host.admin = None
        host._ensure_work_runtime()
        self.assertIs(host.agent.work_store, host.work_store)
        self.assertEqual(host.work_runner.execute, host._execute_work)
        self.assertEqual(set(host.agent.tool_manager._tools), {
            "delegate_background_work", "list_background_work", "cancel_background_work",
        })

    async def test_runner_submits_queued_work_after_recovery(self):
        class AgentWithTools:
            def __init__(self):
                self.turn_store = TurnStore()
                self.tool_manager = ToolManager()

        host = RuntimeHost(Path(tempfile.mkdtemp()))
        host.agent = AgentWithTools()
        host.admin = None
        host._ensure_work_runtime()
        queued = await host.work_store.create(channel="api", user_id="alice",
                                              source="delegated", goal="g")
        submitted = []

        async def execute(record, controls):
            submitted.append(record["id"])

        host.work_runner.execute = execute
        # Mirror the run() startup loop: recover, then requeue.
        from xagent.core.runtime.work_store import recover_work
        await recover_work(host.work_store, host.agent.turn_store)
        for work in await host.work_store.list_all():
            if work.get("status") == "queued":
                await host.work_runner.submit(work["id"])
        deadline = asyncio.get_running_loop().time() + 2.0
        while not submitted and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.005)
        self.assertEqual(submitted, [queued["id"]])
        self.assertEqual((await host.work_store.get(queued["id"]))["status"], "completed")


if __name__ == "__main__":
    unittest.main()
