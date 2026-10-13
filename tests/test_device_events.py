"""Device perception updates shared context without starting a conversation."""

import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient

from xagent.components.message import MessageStorage
from xagent.core.config import AgentConfig, ReplyType
from xagent.integrations.api import ApiChannelAdapter
from xagent.integrations.devices.service import CallRequest
from xagent.interfaces.server.device_routes import register_device_routes
from xagent.interfaces.server.models import ChatInput
from xagent.schemas import MessageType, RoleType
import tests.test_agent_chat_flow as chat_test
import tests.test_device_execution as device_test


def device_event(**overrides):
    return {
        "type": "event", "event_id": "evt_a7f4", "event_type": "touch",
        "context": "头部触摸区域检测到一次轻触。",
        "data": {"area": "head", "gesture": "tap"},
        **overrides,
    }


class DeviceEventProtocolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.storage = MessageStorage(str(self.root / "messages.sqlite3"))
        self.model = chat_test.CapturingModelClient([
            (ReplyType.SIMPLE_REPLY, "Noted the touch."),
            (ReplyType.SIMPLE_REPLY, "I still have that observation."),
        ])
        self.agent = chat_test.AgentChatFlowTests()._build_agent(self.storage, self.model)
        self.service = device_test.device_service()
        self.service.start()
        self.adapter = ApiChannelAdapter(
            self.agent, contacts_file=self.root / "contacts.json",
            tasks_dir=self.root / "tasks", device_service=self.service,
        )
        app = FastAPI()
        self.adapter.register_routes(app)
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.addCleanup(lambda: self.client.portal.call(self.service.stop))

    def connect(self, device_id="desk"):
        return self.client.websocket_connect(
            "/ws/devices/" + device_id,
            headers={"Authorization": "Bearer " + device_id + "-secret"},
        )

    def register(self, socket, device_id="desk"):
        socket.send_json(device_test.manifest(device_id).model_dump())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if self.client.portal.call(lambda: device_id in self.service.devices):
                return
            time.sleep(0.005)
        self.fail("Device did not register")

    def events(self):
        return [
            message for message in self.client.portal.call(self.storage.get_messages, 100)
            if message.type == MessageType.CONTEXT_EVENT
        ]

    def await_events(self, count):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            messages = self.events()
            if len(messages) >= count:
                return messages
            time.sleep(0.005)
        self.fail("Device event was not stored")

    def recent_experience(self, call):
        return next(
            message["content"] for message in call
            if message.get("name") == AgentConfig.RECENT_EXPERIENCE_NAME
        )

    def test_event_without_a_chat_client_is_persisted_without_a_user_or_reply(self):
        with self.connect() as device:
            self.register(device)
            device.send_json(device_event(data={"area": "head", "user_id": "alice"}))
            message, = self.await_events(1)
            self.assertEqual(message.role, RoleType.ENVIRONMENT)
            self.assertEqual(message.content, device_event()["context"])
            self.assertEqual(message.channel, "devices")
            self.assertIsNone(message.sender_id)
            self.assertIsNone(message.recipient_id)
            self.assertIsNone(message.room_name)
            self.assertEqual(message.metadata["source"], "device")
            self.assertEqual(message.metadata["device_id"], "desk")
            self.assertEqual(message.metadata["device_event_id"], "evt_a7f4")
            self.assertEqual(message.metadata["event_type"], "touch")
            self.assertEqual(message.metadata["data"], {"area": "head", "user_id": "alice"})
            self.assertEqual(self.model.calls, [])
            self.assertFalse(self.agent.inbox.busy)
            self.assertEqual(self.agent.inbox.pending_count, 0)
            self.assertEqual(self.agent.memory_handler.experience_messages[0].content, message.content)

    def test_duplicate_event_is_deduplicated_across_reconnection(self):
        with self.connect() as device:
            self.register(device)
            device.send_json(device_event())
            device.send_json(device_event(context="Duplicate payload must not replace the observation."))
            device.send_json(device_event(event_id="barrier-1", context="First barrier."))
            self.assertEqual(len(self.await_events(2)), 2)
        with self.connect() as device:
            self.register(device)
            device.send_json(device_event())
            device.send_json(device_event(event_id="barrier-2", context="Second barrier."))
            messages = self.await_events(3)
            self.assertEqual(len(messages), 3)
            self.assertEqual(messages[0].content, device_event()["context"])
        reopened = MessageStorage(str(self.storage.path))
        self.assertEqual(len(self.client.portal.call(reopened.get_messages, 100)), 3)
        self.assertEqual(self.model.calls, [])

    def test_event_ids_are_scoped_to_devices(self):
        with self.connect() as desk, self.connect("robot") as robot:
            self.register(desk)
            self.register(robot, "robot")
            desk.send_json(device_event())
            robot.send_json(device_event())
            messages = self.await_events(2)
            self.assertEqual({message.metadata["device_id"] for message in messages}, {"desk", "robot"})
            self.assertEqual(len({message.metadata["event_key"] for message in messages}), 2)
        self.assertEqual(self.model.calls, [])

    def test_event_is_shared_by_subsequent_conversations_with_source_visible(self):
        with self.connect() as device:
            self.register(device)
            device.send_json(device_event())
            self.await_events(1)
            for user_id in ("alice", "bob"):
                response = self.client.post("/chat", json={"user_id": user_id, "user_message": "What happened?"})
                self.assertEqual(response.status_code, 200, response.text)
                transcript = self.recent_experience(self.model.calls[-1])
                self.assertIn(device_event()["context"], transcript)
                self.assertIn("[device_id=desk][event_type=touch]", transcript)
                self.assertIn("[ambient context]", transcript)
        messages = self.client.portal.call(self.storage.get_messages, 100)
        users = [message for message in messages if message.role == RoleType.USER]
        self.assertEqual([message.sender_id for message in users], ["alice", "bob"])
        self.assertEqual(len(self.events()), 1)

    def test_events_and_command_results_can_be_interleaved(self):
        with self.connect() as device:
            self.register(device)
            pending = self.client.portal.start_task_soon(
                self.service.call, CallRequest(device_id="desk", capability="custom.execute"),
            )
            command = device.receive_json()
            self.assertEqual(command["type"], "command")
            device.send_json(device_event())
            device.send_json({"type": "result", "command_id": command["command_id"], "ok": True, "data": {"value": 41}})
            device.send_json(device_event(event_id="evt_second", event_type="presence", context="Someone approached."))
            self.assertEqual(pending.result(timeout=2)["data"], {"value": 41})
            self.assertEqual(len(self.await_events(2)), 2)
            self.assertFalse(self.service.pending)
            self.assertEqual(self.model.calls, [])

    def test_invalid_events_and_oversized_frames_fail_closed(self):
        invalid = [
            device_event(event_id=""), device_event(event_type=""),
            device_event(context=" "), device_event(data=[]),
            device_event(device_id="robot"), device_event(user_id="alice"),
            device_event(metadata={"sender_id": "alice"}),
            device_event(context="x" * 65537),
        ]
        for event in invalid:
            with self.subTest(event_keys=list(event)):
                with self.connect() as device:
                    self.register(device)
                    device.send_json(event)
                    with self.assertRaises(WebSocketDisconnect) as closed:
                        device.receive_json()
                    self.assertEqual(closed.exception.code, 1008)
        self.assertEqual(self.events(), [])
        self.assertEqual(self.model.calls, [])

    def test_event_does_not_change_an_in_flight_model_request(self):
        class BlockingModel(chat_test.CapturingModelClient):
            def __init__(self):
                super().__init__([(ReplyType.SIMPLE_REPLY, "First reply."), (ReplyType.SIMPLE_REPLY, "Second reply.")])
                self.started = threading.Event()
                self.release = asyncio.Event()

            async def model_turn_events(self, **kwargs):
                if not self.calls:
                    self.started.set()
                    await self.release.wait()
                async for event in super().model_turn_events(**kwargs):
                    yield event

        model = BlockingModel()
        self.agent.model_client = model
        pending = self.client.portal.start_task_soon(
            self.adapter.chat.run_chat_response, ChatInput(user_id="alice", user_message="First question."),
        )
        try:
            self.assertTrue(model.started.wait(timeout=2))
            with self.connect() as device:
                self.register(device)
                device.send_json(device_event())
                self.await_events(1)
                self.assertTrue(self.agent.inbox.busy)
        finally:
            self.client.portal.call(model.release.set)
        self.assertEqual(pending.result(timeout=3)["reply"], "First reply.")
        self.assertNotIn(device_event()["context"], self.recent_experience(model.calls[0]))
        response = self.client.post("/chat", json={"user_id": "alice", "user_message": "Second question."})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(device_event()["context"], self.recent_experience(model.calls[1]))

    def test_quiescing_ignores_new_events_but_drains_command_results(self):
        with self.connect() as device:
            self.register(device)
            pending = self.client.portal.start_task_soon(
                self.service.call, CallRequest(device_id="desk", capability="custom.execute"),
            )
            command = device.receive_json()
            self.client.portal.call(self.service.quiesce)
            device.send_json(device_event())
            device.send_json({"type": "result", "command_id": command["command_id"], "ok": True})
            self.assertEqual(pending.result(timeout=2)["status"], "success")
            self.assertEqual(self.events(), [])


class DeviceEventOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_buffered_event_from_replaced_connection_is_not_ingested(self):
        class BufferedSocket(device_test.FakeSocket):
            headers = {"authorization": "Bearer desk-secret"}

            def __init__(self):
                super().__init__()
                self.incoming = asyncio.Queue()
                self.waiting_for_event = asyncio.Event()
                self.reads = 0

            async def accept(self):
                pass

            async def receive_text(self):
                self.reads += 1
                if self.reads > 1:
                    self.waiting_for_event.set()
                return await self.incoming.get()

        service = device_test.device_service()
        service.start()
        self.addAsyncCleanup(service.stop)
        on_event = AsyncMock()
        app = FastAPI()
        register_device_routes(app, service, on_event=on_event)
        endpoint = next(route.endpoint for route in app.routes if route.path == "/ws/devices/{device_id}")
        old = BufferedSocket()
        old.incoming.put_nowait(json.dumps(device_test.manifest().model_dump()))
        handler = asyncio.create_task(endpoint(old, "desk"))
        try:
            await asyncio.wait_for(old.waiting_for_event.wait(), timeout=2)
            replacement = await service.register(device_test.FakeSocket(), device_test.manifest())
            old.incoming.put_nowait(json.dumps(device_event()))
            await asyncio.wait_for(handler, timeout=2)
            on_event.assert_not_awaited()
            self.assertIs(service.devices["desk"], replacement)
        finally:
            handler.cancel()
            await asyncio.gather(handler, return_exceptions=True)
