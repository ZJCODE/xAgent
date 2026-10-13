"""Agent-local execution, connection ownership and uncertain side effects."""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import yaml
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from pydantic import ValidationError

from xagent.core.tooling.executor import ToolExecutor
from xagent.core.tooling.guards import ToolGuardResult, WorkspaceShellGuard
from xagent.core.tooling.manager import ToolManager
from xagent.integrations.api import ApiChannelAdapter
from xagent.integrations.devices.config import DeviceConfig
from xagent.integrations.devices.service import CallRequest, DeviceService, DeviceResult, Registration
from xagent.interfaces.base import BaseAgentRunner
from xagent.interfaces.cli.parser import build_parser
from xagent.interfaces.server import AgentHTTPServer
from xagent.interfaces.server.admin_service import AdminService
from xagent.tools.device_tool import create_device_tool
from tests.test_agent_chat_flow import FakeToolCall, InMemoryMessageStorage


def device_config(**overrides):
    return DeviceConfig.model_validate({
        "timeout_seconds": 1,
        **overrides,
    })


def device_service():
    service = DeviceService(device_config())
    for device_id in ("desk", "robot"):
        service.credentials.add(device_id, device_id + "-secret")
    return service


def manifest(device_id="desk", capability="custom.execute", **overrides):
    return Registration.model_validate({
        "type": "register", "device_id": device_id, "name": device_id,
        "location": "office", "metadata": {"implementation": "test executor"},
        "capabilities": [{
            "name": capability, "description": "A device-specific action",
            "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}},
        }],
        **overrides,
    })


class FakeSocket:
    def __init__(self):
        self.commands = asyncio.Queue()
        self.closed = False

    async def send_json(self, command):
        if self.closed:
            raise RuntimeError("closed")
        self.commands.put_nowait(command)

    async def close(self, **kwargs):
        self.closed = True

    async def next_command(self):
        return await asyncio.wait_for(self.commands.get(), timeout=1)


class DeviceExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = device_service()
        self.service.start()
        self.socket = FakeSocket()
        self.connection = await self.service.register(self.socket, manifest())

    async def asyncTearDown(self):
        for connection in list(self.service.devices.values()):
            await self.service.disconnect(connection)

    def call(self, device_id="desk", capability="custom.execute", **kwargs):
        return asyncio.create_task(self.service.call(CallRequest(
            device_id=device_id, capability=capability, **kwargs,
        )))

    def resolve(self, command, *, connection=None, **result):
        self.service.resolve(connection or self.connection, DeviceResult(
            type="result", command_id=command["command_id"], ok=True, **result,
        ))

    async def test_arbitrary_capability_and_nested_arguments_round_trip(self):
        params = {"value": 7, "path": ["arm", "joint"], "options": {"enabled": True}}
        task = self.call(params=params)
        command = await self.socket.next_command()
        self.assertEqual(command["capability"], "custom.execute")
        self.assertEqual(command["params"], params)
        self.resolve(command, data={"position": [1, 2, 3]})
        self.assertEqual((await task)["data"], {"position": [1, 2, 3]})
        self.assertFalse(self.service.pending)

    async def test_offline_and_unadvertised_capability_never_send(self):
        offline = await self.call(device_id="robot")
        unavailable = await self.call(capability="not.declared")
        self.assertEqual(offline["code"], "device_unavailable")
        self.assertEqual(unavailable["code"], "capability_unavailable")
        self.assertTrue(self.socket.commands.empty())

    async def test_timeout_is_unknown_and_late_result_is_not_replayed(self):
        self.service.config.timeout_seconds = 0.1
        task = self.call()
        command = await self.socket.next_command()
        result = await task
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["command_id"], command["command_id"])
        self.resolve(command, data="late")
        self.assertFalse(self.service.pending)
        self.assertTrue(self.socket.commands.empty())

    async def test_disconnect_after_sending_is_unknown(self):
        task = self.call()
        await self.socket.next_command()
        await self.service.disconnect(self.connection)
        result = await task
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["code"], "device_disconnected")
        self.assertFalse(self.service.devices)

    async def test_reconnect_does_not_replay_and_old_cleanup_cannot_remove_new_connection(self):
        old_task = self.call()
        await self.socket.next_command()
        new_socket = FakeSocket()
        new_connection = await self.service.register(new_socket, manifest(capability="new.action"))
        self.assertEqual((await old_task)["status"], "unknown")
        await self.service.disconnect(self.connection)
        self.assertIs(self.service.devices["desk"], new_connection)
        self.assertTrue(new_socket.commands.empty())
        task = self.call(capability="new.action")
        command = await new_socket.next_command()
        self.resolve(command, data="forged old result")
        self.assertFalse(task.done())
        self.resolve(command, connection=new_connection, data="new result")
        self.assertEqual((await task)["data"], "new result")

    async def test_other_device_cannot_complete_a_command(self):
        robot = await self.service.register(FakeSocket(), manifest("robot"))
        task = self.call()
        command = await self.socket.next_command()
        self.resolve(command, connection=robot, data="wrong device")
        self.assertFalse(task.done())
        self.resolve(command, data="right device")
        self.assertEqual((await task)["data"], "right device")

    async def test_commands_serialize_per_device_but_other_devices_continue(self):
        robot_socket = FakeSocket()
        robot = await self.service.register(robot_socket, manifest("robot"))
        first = self.call()
        first_command = await self.socket.next_command()
        second = self.call(params={"value": 2})
        other = self.call(device_id="robot")
        robot_command = await robot_socket.next_command()
        self.assertTrue(self.socket.commands.empty())
        self.resolve(robot_command, connection=robot)
        self.assertEqual((await other)["status"], "success")
        self.resolve(first_command)
        self.assertEqual((await first)["status"], "success")
        second_command = await self.socket.next_command()
        self.assertEqual(second_command["params"], {"value": 2})
        self.resolve(second_command)
        self.assertEqual((await second)["status"], "success")

    async def test_queued_timeout_is_not_sent(self):
        first = self.call()
        command = await self.socket.next_command()
        self.service.config.timeout_seconds = 0.1
        second = await self.call()
        self.assertEqual(second["status"], "error")
        self.assertEqual(second["code"], "busy")
        self.resolve(command)
        await first
        self.assertTrue(self.socket.commands.empty())
        self.assertFalse(self.service.pending)

    async def test_cancelled_http_wait_cleans_pending_without_retry(self):
        task = self.call()
        command = await self.socket.next_command()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.resolve(command)
        self.assertFalse(self.service.pending)
        self.assertTrue(self.socket.commands.empty())

    async def test_invalid_or_oversized_arguments_are_rejected_before_sending(self):
        for params in ({"value": float("nan")}, {"value": "x" * 65536}):
            result = await self.call(params=params)
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["code"], "invalid_arguments")
        self.assertTrue(self.socket.commands.empty())

    async def test_unicode_payload_limit_matches_utf8_wire_encoding(self):
        text = "温" * 12000
        task = self.call(params={"text": text})
        command = await self.socket.next_command()
        self.assertLess(len(json.dumps(command, ensure_ascii=False).encode("utf-8")), 65536)
        self.assertEqual(command["params"]["text"], text)
        self.resolve(command)
        self.assertEqual((await task)["status"], "success")


    async def test_quiesce_rejects_new_commands_and_drains_accepted_result(self):
        task = self.call()
        command = await self.socket.next_command()
        stopping = asyncio.create_task(self.service.stop(drain_timeout=1))
        await asyncio.sleep(0)
        rejected = await self.call()
        self.assertEqual(rejected["code"], "device_unavailable")
        self.assertFalse(stopping.done())
        self.resolve(command, data="completed during drain")
        self.assertEqual((await task)["status"], "success")
        await stopping
        self.assertTrue(self.service.resources_closed)
        self.assertFalse(self.service.accepting)
        self.service.start()
        self.assertEqual(self.service.list_devices()["devices"], [])

    async def test_stop_completes_unknown_call_even_when_send_is_stalled(self):
        class StalledSocket(FakeSocket):
            async def send_json(inner, command):
                inner.commands.put_nowait(command)
                await asyncio.Event().wait()
        socket = StalledSocket()
        await self.service.register(socket, manifest())
        self.service.config.timeout_seconds = 120
        task = self.call()
        await socket.next_command()
        await asyncio.wait_for(self.service.stop(), timeout=1)
        self.assertEqual((await task)["status"], "unknown")
        self.assertTrue(self.service.resources_closed)
        self.assertTrue(socket.closed)

    async def test_two_agents_have_independent_device_state(self):
        other = device_service()
        other.start()
        self.assertEqual(other.list_devices()["devices"], [])
        self.assertEqual((await other.call(CallRequest(device_id="desk", capability="custom.execute")))["code"], "device_unavailable")
        self.assertIn("desk", self.service.devices)
        await other.stop()


class ToolChatAgent:
    model = "test-model"
    def __init__(self, service):
        self.message_storage = InMemoryMessageStorage()
        self.tool_manager = ToolManager([create_device_tool(service)])
        self.tools = self.tool_manager.tools
        self.executor = ToolExecutor(self.tool_manager, self.message_storage, None)

    async def chat_events(self, user_message, user_id, channel, **kwargs):
        if user_message == "hello":
            content = "hello"
        else:
            messages = []
            await self.executor.handle_tool_calls([FakeToolCall("device", user_message)], messages, user_id=user_id, channel=channel)
            content = messages[-1]["content"]
        yield {"type": "message_done", "message_id": "m1", "phase": "final", "content": content}
        yield {"type": "done"}


class DeviceProtocolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.service = device_service()
        self.service.start()
        agent = ToolChatAgent(self.service)
        adapter = ApiChannelAdapter(agent, contacts_file=root / "contacts.json", tasks_dir=root / "tasks", device_service=self.service)
        server = AgentHTTPServer(config_dir=str(root), agent=agent, api_adapter=adapter)
        self.client = TestClient(server.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.addCleanup(lambda: self.client.portal.call(self.service.stop))

    def connect(self, device_id="desk"):
        return self.client.websocket_connect("/ws/devices/" + device_id, headers={"Authorization": "Bearer " + device_id + "-secret"})

    def listed(self, **filters):
        return self.client.portal.call(lambda: self.service.list_devices(**filters))["devices"]

    def await_registration(self, device_id):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            rows = self.listed(device_id=device_id)
            if rows:
                return rows[0]
            time.sleep(0.005)
        self.fail("Device did not register")

    def chat_result(self, chat):
        result = None
        while True:
            event = chat.receive_json()
            if event["type"] == "message_done":
                result = event["content"]
            if event["type"] == "done":
                return result

    def test_chat_and_execution_use_independent_sockets_on_same_api(self):
        with self.connect() as device, self.client.websocket_connect("/ws/chat") as chat:
            device.send_json(manifest().model_dump())
            self.await_registration("desk")
            chat.send_json({"user_id": "alice", "user_message": json.dumps({"action": "call", "device_id": "desk", "capability": "custom.execute", "params": {"nested": {"value": 8}}})})
            command = device.receive_json()
            self.assertEqual(command["params"], {"nested": {"value": 8}})
            device.send_json({"type": "result", "command_id": command["command_id"], "ok": True, "data": {"value": 8}})
            self.assertEqual(json.loads(self.chat_result(chat))["data"], {"value": 8})
            chat.send_json({"user_id": "alice", "user_message": "hello", "stream": False})
            self.assertEqual(self.chat_result(chat), "hello")
        self.assertEqual(self.listed(), [])
        self.assertEqual(self.client.get("/devices").status_code, 404)
        self.assertEqual(self.client.post("/call", json={}).status_code, 404)

    def test_discovery_filters_are_available_to_any_chat_user(self):
        with self.connect() as desk, self.connect("robot") as robot, self.client.websocket_connect("/ws/chat") as chat:
            desk.send_json(manifest().model_dump())
            robot.send_json(manifest("robot", capability="arm.move").model_dump())
            self.await_registration("desk")
            self.await_registration("robot")
            self.assertEqual([row["device_id"] for row in self.listed(location="office", capability="arm.move")], ["robot"])
            chat.send_json({"user_id": "mallory", "user_message": '{"action":"list"}'})
            self.assertEqual({row["device_id"] for row in json.loads(self.chat_result(chat))["devices"]}, {"desk", "robot"})
            chat.send_json({"user_id": "alice", "user_message": '{"action":"list","device_id":"desk"}'})
            listed = self.chat_result(chat)
            self.assertEqual(json.loads(listed)["devices"][0]["device_id"], "desk")
            self.assertNotIn("secret", listed)

    def test_device_credential_cannot_register_another_identity(self):
        with self.assertRaises(WebSocketDisconnect) as caught:
            with self.client.websocket_connect("/ws/devices/robot", headers={"Authorization": "Bearer desk-secret"}):
                pass
        self.assertEqual(caught.exception.code, 1008)
        with self.connect() as socket:
            socket.send_json(manifest("robot").model_dump())
            with self.assertRaises(WebSocketDisconnect) as caught:
                socket.receive_json()
            self.assertEqual(caught.exception.code, 1008)
        self.assertEqual(self.listed(), [])

    def test_invalid_registration_and_result_fail_closed(self):
        with self.connect() as socket:
            registration = manifest().model_dump()
            registration["capabilities"] *= 2
            socket.send_json(registration)
            with self.assertRaises(WebSocketDisconnect):
                socket.receive_json()
        with self.connect() as socket:
            socket.send_json(manifest().model_dump())
            self.await_registration("desk")
            socket.send_json({"type": "result", "command_id": "fake", "ok": "true"})
            with self.assertRaises(WebSocketDisconnect):
                socket.receive_json()
        self.assertEqual(self.listed(), [])

    def test_oversized_and_non_json_frames_are_rejected(self):
        for raw in ('x' * 65537, '{"value":NaN}'):
            with self.connect() as socket:
                socket.send_text(raw)
                with self.assertRaises(WebSocketDisconnect) as caught:
                    socket.receive_json()
                self.assertEqual(caught.exception.code, 1008)
        self.assertEqual(self.listed(), [])

    def test_api_unavailable_does_not_accept_authenticated_device(self):
        self.client.portal.call(self.service.stop)
        with self.connect() as socket:
            with self.assertRaises(WebSocketDisconnect) as caught:
                socket.receive_json()
            self.assertEqual(caught.exception.code, 1013)
        self.assertEqual(self.listed(), [])


class DeviceToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_tool_discovers_and_calls_without_http(self):
        service = device_service()
        service.start()
        socket = FakeSocket()
        connection = await service.register(socket, manifest(capability="robot.custom"))
        tool = create_device_tool(service)
        self.assertEqual(tool.tool_spec["function"]["name"], "device")
        self.assertEqual(tool.tool_spec["function"]["parameters"]["properties"]["params"]["type"], "object")
        with patch("httpx.AsyncClient", side_effect=AssertionError("HTTP client used for device execution")):
            rows = await tool(action="list", capability="robot.custom")
            self.assertEqual(rows["devices"][0]["capabilities"][0]["name"], "robot.custom")
            task = asyncio.create_task(tool(action="call", device_id="desk", capability="robot.custom", params={"steps": [1, 2]}))
            command = await socket.next_command()
            service.resolve(connection, DeviceResult(type="result", command_id=command["command_id"], ok=True, data="executed"))
            self.assertEqual((await task)["data"], "executed")
        await service.stop()

    async def test_invalid_tool_arguments_never_dispatch(self):
        service = device_service()
        tool = create_device_tool(service)
        with patch.object(service, "call", side_effect=AssertionError("Invalid command dispatched")):
            for args in (
                {"action": "call", "location": "office", "capability": "x"},
                {"action": "call", "device_id": "desk", "capability": "x", "params": {"value": float("nan")}},
                {"action": "call", "device_id": "bad/id", "capability": "x"},
                {"action": "list", "params": {}}, {"action": "invalid"},
            ):
                self.assertEqual((await tool(**args))["status"], "error")

    async def test_all_callers_can_discover_and_call_while_attribution_is_preserved(self):
        service = device_service()
        service.start()
        self.addAsyncCleanup(service.stop)
        socket = FakeSocket()
        connection = await service.register(socket, manifest())
        contexts = []
        class AttributionGuard:
            async def pre_execute(self, ctx):
                contexts.append((ctx.user_id, ctx.channel, ctx.room_name))
                return ToolGuardResult.allow()
            async def post_execute(self, ctx, result):
                return result
        executor = ToolExecutor(
            tool_manager=ToolManager([create_device_tool(service)]), message_storage=InMemoryMessageStorage(), client=None,
            guards=[AttributionGuard()],
        )
        for user_id, channel, room_name in (("alice", "cli", None), ("new-user", "api", None), ("other", "feishu", "group"), ("", None, None)):
            messages = []
            attribution = {"user_id": user_id, "channel": channel, "room_name": room_name}
            await executor.handle_tool_calls([FakeToolCall("device", '{"action":"list"}')], messages, **attribution)
            self.assertEqual(json.loads(messages[-1]["content"])["devices"][0]["device_id"], "desk")
            task = asyncio.create_task(executor.handle_tool_calls([
                FakeToolCall("device", '{"action":"call","device_id":"desk","capability":"custom.execute"}'),
            ], messages, **attribution))
            command = await socket.next_command()
            service.resolve(connection, DeviceResult(type="result", command_id=command["command_id"], ok=True, data=user_id))
            await task
            self.assertEqual(json.loads(messages[-1]["content"])["status"], "success")
            self.assertEqual(contexts[-2:], [(user_id, channel, room_name)] * 2)


class DeviceConfigurationTests(unittest.TestCase):
    def test_configuration_is_optional_with_default_media_limits(self):
        self.assertEqual(DeviceConfig().timeout_seconds, 10)
        self.assertEqual(DeviceConfig().media.max_image_bytes, 10 * 1024 * 1024)
        self.assertEqual(DeviceConfig().media.max_file_bytes, 50 * 1024 * 1024)
        for overrides in (
            {"timeout_seconds": float("inf")},
            {"timeout_seconds": True}, {"unexpected": True},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValidationError):
                device_config(**overrides)

    def test_agent_and_tool_share_service_without_offline_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {"provider": {"name": "openai", "model": "test-model", "api_key": "test-key"}}
            (root / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
            (root / "identity.md").write_text("Test identity.", encoding="utf-8")
            with patch.object(BaseAgentRunner, "_initialize_client", return_value=object()), patch("httpx.AsyncClient", side_effect=AssertionError("Network during bootstrap")):
                runner = BaseAgentRunner(config_dir=directory)
            with patch("xagent.interfaces.base.DeviceService", side_effect=AssertionError("Offline view created device service")):
                admin = AdminService(config_dir=directory)
                self.assertEqual(admin._get_agent_tool_names(), list(runner.agent.tools))
            self.assertIn("device", runner.agent.tools)
            self.assertNotIn("device_list", runner.agent.tools)
            self.assertFalse(runner.device_service.accepting)
            self.assertTrue(runner.device_service.resources_closed)
            self.assertEqual(len(runner.agent.tool_executor.guards), 1)
            self.assertIsInstance(runner.agent.tool_executor.guards[0], WorkspaceShellGuard)

    def test_standalone_gateway_command_is_removed(self):
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            build_parser().parse_args(["device-gateway", "--config", "unused.yaml"])


if __name__ == "__main__":
    unittest.main()
