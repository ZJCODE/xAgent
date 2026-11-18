"""Silent device decisions, stale work, and existing command/result transport."""

import asyncio
import json
import logging
import tempfile
import unittest
from pathlib import Path

from xagent.components.message import MessageStorage
from xagent.core.config import ReplyType
from xagent.core.handlers.model import ChatToolCall
from xagent.integrations.api import ApiChannelAdapter
from xagent.integrations.devices.service import DeviceEvent, DeviceResult
from xagent.schemas import Message, RoleType
import tests.test_agent_chat_flow as chat_test
from tests.test_device_execution import device_service, FakeSocket, manifest


def action():
    return (ReplyType.TOOL_CALL, [ChatToolCall(
        call_id="decision", name="device", arguments=json.dumps({
            "action": "call", "device_id": "desk", "capability": "light.set", "params": {"value": 1},
        }),
    )])


class DeviceObservationActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = MessageStorage(str(self.root / "messages.sqlite3"))
        self.model = chat_test.CapturingModelClient([])
        self.agent = chat_test.AgentChatFlowTests()._build_agent(self.storage, self.model)
        self.service = device_service()
        self.service.start()
        self.socket = FakeSocket()
        self.connection = await self.service.register(self.socket, manifest(capability="light.set"))
        self.adapter = ApiChannelAdapter(
            self.agent, contacts_file=self.root / "contacts.json", tasks_dir=self.root / "tasks",
            device_service=self.service, logger=logging.getLogger(__name__),
        )
        self.worker = self.adapter.device_observations

    async def asyncTearDown(self):
        await self.adapter.stop()
        await self.service.stop()

    async def report(self, event_id="applause", context="Audience started applauding."):
        await self.adapter.observe_device_event("desk", DeviceEvent(
            type="event", event_id=event_id, event_type="audience", context=context,
        ))

    async def finish(self):
        await asyncio.wait_for(asyncio.shield(self.worker.task), timeout=2)

    async def test_background_then_action_uses_goal_and_previous_result(self):
        await self.storage.add_messages(Message(content="Solo in progress. At curtain call, set the lights.", role=RoleType.USER))
        self.model.responses = [(ReplyType.SIMPLE_REPLY, "Background."), action(), (ReplyType.SIMPLE_REPLY, "Already done.")]
        await self.report()
        await self.finish()
        self.assertTrue(self.socket.commands.empty())
        await self.storage.add_messages(Message(content="We are at curtain call now.", role=RoleType.USER))
        await self.report("again")
        command = await self.socket.next_command()
        self.assertEqual(command["capability"], "light.set")
        await self.service.resolve_result(self.connection, DeviceResult(
            type="result", command_id=command["command_id"], ok=True, data={"lights": "on"},
        ))
        await self.finish()
        await self.report("continuing", "Audience still applauding.")
        await self.finish()
        history = json.dumps(self.model.calls[-1])
        self.assertIn("curtain call", history)
        self.assertIn("device_action_result", history)
        self.assertIn(command["command_id"], history)
        self.assertTrue(self.socket.commands.empty())
        messages = await self.storage.get_messages(100)
        self.assertFalse(any(m.role == RoleType.ASSISTANT for m in messages))

    async def test_concurrent_duplicate_only_schedules_once(self):
        self.model.responses = [(ReplyType.SIMPLE_REPLY, "Background.")]
        await asyncio.gather(*(self.report() for _ in range(10)))
        await self.finish()
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(len(await self.storage.get_messages(100)), 1)

    async def test_user_revision_discards_inflight_action(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.agent.decide_device_action

        async def blocked(*args):
            entered.set()
            await release.wait()
            return await original(*args)

        self.agent.decide_device_action = blocked
        self.model.responses = [(ReplyType.SIMPLE_REPLY, "Cancelled."), action(),
                                (ReplyType.SIMPLE_REPLY, "Background.")]
        await self.report()
        await entered.wait()
        await self.agent.chat("Cancel automatic lighting.", user_id="alice")
        release.set()
        await self.finish()
        self.assertTrue(self.socket.commands.empty())
        self.assertEqual(len(self.model.calls), 3)
        self.assertIn("Cancel automatic lighting", json.dumps(self.model.calls[-1]))

    async def test_repeated_samples_coalesce_and_transitions_rebuild_context(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.agent.decide_device_action

        async def blocked(*args):
            entered.set()
            await release.wait()
            return await original(*args)

        self.agent.decide_device_action = blocked
        self.model.responses = [action(), (ReplyType.SIMPLE_REPLY, "Background.")]
        await self.report()
        await entered.wait()
        for index in range(8):
            await self.report(str(index))
        self.assertIsNone(self.worker.pending)
        await self.report("stop", "Applause stopped.")
        await self.report("restart", "Audience started applauding again.")
        release.set()
        await self.finish()
        self.assertEqual(len(self.model.calls), 2)
        self.assertIn("Applause stopped", json.dumps(self.model.calls[-1]))
        self.assertTrue(self.socket.commands.empty())

    async def test_unknown_is_recorded_without_retry(self):
        self.model.responses = [action()]
        self.service.config.timeout_seconds = 0.1
        await self.report()
        command = await self.socket.next_command()
        await self.finish()
        messages = await self.storage.get_messages(100)
        result = json.loads(messages[-1].content)["result"]
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["command_id"], command["command_id"])
        self.assertTrue(self.socket.commands.empty())
        self.assertEqual(len(self.model.calls), 1)

    async def test_stale_action_waiting_for_connection_is_not_sent(self):
        self.model.responses = [action(), (ReplyType.SIMPLE_REPLY, "Cancelled."),
                                (ReplyType.SIMPLE_REPLY, "Background.")]
        await self.connection.lock.acquire()
        try:
            await self.report()
            for _ in range(100):
                if self.service.pending:
                    break
                await asyncio.sleep(0.001)
            self.assertTrue(self.service.pending)
            await self.agent.chat("Cancel automatic lighting.", user_id="alice")
        finally:
            self.connection.lock.release()
        await self.finish()
        self.assertTrue(self.socket.commands.empty())
        self.assertEqual(len(self.model.calls), 3)

    async def test_plain_observe_never_triggers_worker(self):
        result = await self.agent.observe("Ignored speech.", source="device")
        self.assertFalse(result.replied)
        self.assertIsNone(self.worker.task)
