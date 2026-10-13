"""Real local sockets exercise device ownership and Runtime/API lifecycle."""

import asyncio
import inspect
import ipaddress
import json
import os
import socket
import ssl
import sys
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import websockets
import yaml
import httpx
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from xagent.core.config import AgentConfig, ReplyType
from xagent.core.runtime.client import RuntimeClient
from xagent.integrations.devices.service import CallRequest
from xagent.integrations.devices.credentials import DeviceCredentials
from xagent.schemas import MessageType, RoleType
import tests.test_agent_chat_flow as chat_test
import tests.test_runtime_host as runtime_test
import tests.test_device_execution as device_test
import tests.test_device_media as media_test


class DeviceRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hosts = []
        self.connection_options = {"proxy": None} if "proxy" in inspect.signature(websockets.connect).parameters else {}

    async def asyncTearDown(self):
        for host, task in reversed(self.hosts):
            await host.stop()
            await asyncio.wait_for(task, timeout=20)
            self.assertTrue(host.device_service.resources_closed)

    async def wait_for(self, condition):
        deadline = time.monotonic() + 3
        while not condition():
            if time.monotonic() > deadline:
                self.fail("Runtime condition did not become ready")
            await asyncio.sleep(0.01)

    async def start_host(self, responses, *, seed_credentials=True):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        runtime_test.write_config(root)
        config = yaml.safe_load((root / "config.yaml").read_text())
        config["channels"]["api"] = {"enabled": True, "host": "127.0.0.1", "port": port}
        (root / "config.yaml").write_text(yaml.safe_dump(config))
        if seed_credentials:
            DeviceCredentials(root / ".devices.json").add("desk", "desk-secret")
        host, task = await runtime_test.RuntimeTransportTests._start_host(self, root, chat_test.CapturingModelClient(responses))
        self.hosts.append((host, task))
        await self.wait_for(lambda: host.channel_status["api"]["state"] == "connected")
        self.assertIs(host.device_service, host.runner.device_service)
        self.assertIs(host.device_service, host.api.device_service)
        return host, task, root, f"ws://127.0.0.1:{port}"

    def device_socket(self, url):
        return websockets.connect(url + "/ws/devices/desk", additional_headers={"Authorization": "Bearer desk-secret"}, **self.connection_options)

    async def register(self, device, host):
        await device.send(json.dumps(device_test.manifest().model_dump()))
        await self.wait_for(lambda: "desk" in host.device_service.devices)

    async def chat_events(self, chat):
        events = []
        while True:
            event = json.loads(await asyncio.wait_for(chat.recv(), timeout=5))
            events.append(event)
            if event["type"] == "done":
                return events

    async def test_real_agent_chat_tool_call_receives_result_over_separate_device_socket(self):
        call = chat_test.FakeToolCall("device", json.dumps({
            "action": "call", "device_id": "desk", "capability": "custom.execute", "params": {"value": 41},
        }))
        host, _, _, url = await self.start_host([
            (ReplyType.TOOL_CALL, [call]), (ReplyType.SIMPLE_REPLY, "Device set to 41"),
        ])
        async with self.device_socket(url) as device, websockets.connect(url + "/ws/chat", **self.connection_options) as chat:
            await self.register(device, host)
            await chat.send(json.dumps({"user_id": "alice", "user_message": "Set the device to 41", "stream": False}))
            command = json.loads(await asyncio.wait_for(device.recv(), timeout=5))
            self.assertEqual(command["params"], {"value": 41})
            await device.send(json.dumps({"type": "result", "command_id": command["command_id"], "ok": True, "data": {"value": 41}}))
            events = await self.chat_events(chat)
            self.assertTrue(any(event.get("content") == "Device set to 41" for event in events))
            self.assertFalse(host.device_service.pending)

    async def test_media_capture_view_and_delivery_over_verified_tls(self):
        identifier = str(uuid.uuid4())
        path = f"assets/inbound/devices/{identifier}/content.png"
        responses = [
            (ReplyType.TOOL_CALL, [chat_test.FakeToolCall(name="device", call_id="capture", arguments=json.dumps({
                "action": "call", "device_id": "desk", "capability": "camera.capture", "params": {}, "timeout_seconds": 90,
            }))]),
            (ReplyType.TOOL_CALL, [chat_test.FakeToolCall(name="see_image", call_id="view", arguments=json.dumps({"path": path}))]),
            (ReplyType.TOOL_CALL, [chat_test.FakeToolCall(name="attach_artifact", call_id="deliver", arguments=json.dumps({"path": path, "caption": "Device photo"}))]),
            (ReplyType.SIMPLE_REPLY, "The photo shows the desk."),
        ]
        host, _, root, _ = await self.start_host([])
        model = chat_test.SnapshotModelClient(responses)
        host.agent.model_client = model
        # A temporary TLS listener models termination on the same Runtime app.
        # The test explicitly trusts its CA and still verifies the hostname.
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
        certificate, private_key = root / "test-cert.pem", root / "test-key.pem"
        certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        private_key.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        context = ssl.create_default_context(cafile=str(certificate))
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(host._servers["api"].config.app,
            ssl_certfile=str(certificate), ssl_keyfile=str(private_key), log_level="error", lifespan="off"))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            await self.wait_for(lambda: server.started)
            base = f"wss://127.0.0.1:{port}"
            async with websockets.connect(base + "/ws/devices/desk", ssl=context,
                additional_headers={"Authorization": "Bearer desk-secret"}, **self.connection_options) as device:
                await device.send(json.dumps(device_test.manifest(capability="camera.capture").model_dump()))
                await self.wait_for(lambda: "desk" in host.device_service.devices)
                async with websockets.connect(base + "/ws/chat", ssl=context, **self.connection_options) as chat:
                    await chat.send(json.dumps({"user_id": "alice", "user_message": "Take a photo, inspect it and send it to me", "stream": False}))
                    command = json.loads(await asyncio.wait_for(device.recv(), 5))
                    self.assertEqual(command["capability"], "camera.capture")
                    data = media_test.picture()
                    async with httpx.AsyncClient(verify=context, trust_env=False) as client:
                        response = await client.put(f"https://127.0.0.1:{port}/api/device-media/desk/{identifier}?filename=photo.png",
                            content=data, headers={"Authorization": "Bearer desk-secret", "Content-Type": "image/png"})
                    self.assertEqual(response.status_code, 201)
                    await device.send(json.dumps({"type": "event", "event_id": "photo-event", "event_type": "camera", "context": "The camera saved a photo.",
                        "attachments": [{"attachment_id": identifier}]}))
                    await device.send(json.dumps({"type": "result", "command_id": command["command_id"], "ok": True,
                        "data": {"captured": True}, "attachments": [{"attachment_id": identifier}]}))
                    events = await self.chat_events(chat)
                    displayed = [attachment for event in events for attachment in event.get("attachments", [])]
                    self.assertTrue(any(item["path"] == path for item in displayed))
                    self.assertEqual((root / "workspace" / path).read_bytes(), data)
                    self.assertNotIn("data:image/", json.dumps(model.calls[0]))
                    self.assertNotIn("data:image/", json.dumps(model.calls[1]))
                    self.assertIn("data:image/", json.dumps(model.calls[2]))
                    self.assertEqual(len(model.calls), 4)
                    self.assertIn("data:image/", json.dumps(model.calls[3]))
                    self.assertEqual(len(displayed), 1)
                    self.assertTrue(any(event.get("content") == "The photo shows the desk." for event in events))
                    self.assertEqual(sum(event["type"] == "done" for event in events), 1)
                    observation = await host.agent.message_storage.get_message_by_metadata("device_event_id", "photo-event")
                    self.assertEqual(observation.metadata["attachments"][0]["path"], path)
                    self.assertIn(path, observation.content)
                    self.assertIsNone(observation.sender_id)
                    self.assertFalse(host.device_service.uploads)
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, 10)
            listener.close()

    async def test_environment_event_survives_runtime_restart_and_remains_shared(self):
        async def recorded(host, event_id):
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                message = await host.agent.message_storage.get_message_by_metadata("device_event_id", event_id)
                if message is not None:
                    return message
                await asyncio.sleep(0.01)
            self.fail("Runtime did not persist the device observation")

        event = {
            "type": "event", "event_id": "touch-1", "event_type": "touch",
            "context": "The head sensor detected a tap.", "data": {"area": "head"},
        }
        first, task, root, url = await self.start_host([])
        async with self.device_socket(url) as device:
            await self.register(device, first)
            await device.send(json.dumps(event))
            message = await recorded(first, "touch-1")
            self.assertEqual(message.role, RoleType.ENVIRONMENT)
            self.assertIsNone(message.sender_id)
            self.assertIsNone(message.recipient_id)
            self.assertEqual(first.agent.model_client.calls, [])
        await first.stop()
        await asyncio.wait_for(task, timeout=20)
        self.hosts.pop()

        model = chat_test.CapturingModelClient([(ReplyType.SIMPLE_REPLY, "I noticed the tap.")])
        second, task = await runtime_test.RuntimeTransportTests._start_host(self, root, model)
        self.hosts.append((second, task))
        await self.wait_for(lambda: second.channel_status["api"]["state"] == "connected")
        async with self.device_socket(url) as device:
            await self.register(device, second)
            await device.send(json.dumps({**event, "context": "Duplicate content must not replace the event."}))
            await device.send(json.dumps({**event, "event_id": "touch-2"}))
            await recorded(second, "touch-2")
            observations = [
                item for item in await second.agent.message_storage.get_messages(100)
                if item.type == MessageType.CONTEXT_EVENT
            ]
            self.assertEqual(len(observations), 2)
            self.assertEqual(observations[0].content, event["context"])
            self.assertEqual(model.calls, [])
            events = [item async for item in RuntimeClient(root).chat_events(user_id="alice", user_message="What happened?")]
            self.assertTrue(any(item.get("content") == "I noticed the tap." for item in events))
            transcript = next(item["content"] for item in model.calls[0] if item.get("name") == AgentConfig.RECENT_EXPERIENCE_NAME)
            self.assertIn(event["context"], transcript)
            self.assertIn("[device_id=desk][event_type=touch]", transcript)

    async def test_api_disconnect_resolves_unknown_and_private_chat_survives_then_device_reregisters(self):
        host, _, root, url = await self.start_host([(ReplyType.SIMPLE_REPLY, "Local chat still works")])
        host.channel_drain_timeout = 0.05
        service = host.device_service
        async with self.device_socket(url) as device:
            await self.register(device, host)
            pending = asyncio.create_task(service.call(CallRequest(device_id="desk", capability="custom.execute")))
            await asyncio.wait_for(device.recv(), timeout=3)
            with self.assertNoLogs("uvicorn.error", level="ERROR"):
                await host.disconnect_channel("api")
            self.assertEqual((await pending)["status"], "unknown")
            self.assertFalse(service.accepting)
            self.assertTrue(service.resources_closed)
            self.assertEqual(host.state, "running")
            result = await host.agent.tools["device"](action="call", device_id="desk", capability="custom.execute")
            self.assertEqual(result["code"], "device_unavailable")
            events = [event async for event in RuntimeClient(root).chat_events(user_id="alice", user_message="hello")]
            self.assertTrue(any(event.get("content") == "Local chat still works" for event in events))
        await host.connect_channel("api")
        await self.wait_for(lambda: host.channel_status["api"]["state"] == "connected")
        self.assertIs(host.device_service, service)
        self.assertEqual(service.list_devices()["devices"], [])
        async with self.device_socket(url) as device:
            await self.register(device, host)
            self.assertEqual(len(service.devices), 1)
            self.assertFalse(service.pending)

    async def test_idle_devices_close_without_waiting_for_full_drain_deadline(self):
        host, _, _, url = await self.start_host([])
        async with self.device_socket(url) as device:
            await self.register(device, host)
            started = time.monotonic()
            await host.disconnect_channel("api")
            self.assertLess(time.monotonic() - started, 2)
            self.assertTrue(host.device_service.resources_closed)

    async def test_api_disconnect_closes_device_waiting_for_registration(self):
        host, _, _, url = await self.start_host([])
        host.channel_drain_timeout = 0.05
        async with self.device_socket(url):
            with self.assertNoLogs("uvicorn.error", level="ERROR"):
                await host.disconnect_channel("api")
            self.assertTrue(host.device_service.resources_closed)
            self.assertEqual(host.channel_status["api"]["state"], "disconnected")

    async def test_two_runtimes_do_not_share_devices_or_api_lifecycle(self):
        first, _, _, first_url = await self.start_host([])
        second, _, _, second_url = await self.start_host([])
        self.assertIsNot(first.device_service, second.device_service)
        async with self.device_socket(first_url) as device:
            await self.register(device, first)
            self.assertEqual(second.device_service.list_devices()["devices"], [])
            result = await second.agent.tools["device"](action="call", device_id="desk", capability="custom.execute")
            self.assertEqual(result["code"], "device_unavailable")
            await first.disconnect_channel("api")
            self.assertEqual(second.channel_status["api"]["state"], "connected")
            self.assertTrue(second.device_service.accepting)
        async with self.device_socket(second_url) as device:
            await self.register(device, second)
            self.assertEqual(len(second.device_service.devices), 1)
            self.assertEqual(first.device_service.list_devices()["devices"], [])

    async def test_runtime_stop_drains_accepted_tool_call_before_closing_device(self):
        call = chat_test.FakeToolCall("device", '{"action":"call","device_id":"desk","capability":"custom.execute"}')
        host, host_task, _, url = await self.start_host([
            (ReplyType.TOOL_CALL, [call]), (ReplyType.SIMPLE_REPLY, "Accepted work completed"),
        ])
        async with self.device_socket(url) as device, websockets.connect(url + "/ws/chat", **self.connection_options) as chat:
            await self.register(device, host)
            await chat.send(json.dumps({"user_id": "alice", "user_message": "Execute once"}))
            command = json.loads(await asyncio.wait_for(device.recv(), timeout=5))
            await host.stop()
            await asyncio.sleep(0.01)
            self.assertFalse(host_task.done())
            await device.send(json.dumps({"type": "result", "command_id": command["command_id"], "ok": True, "data": "finished"}))
            events = await self.chat_events(chat)
            self.assertTrue(any(event.get("content") == "Accepted work completed" for event in events))
        await asyncio.wait_for(host_task, timeout=5)
        self.assertTrue(host.device_service.resources_closed)

    async def test_runnable_software_executor_handles_calls_and_reconnects(self):
        host, host_task, root, url = await self.start_host([], seed_credentials=False)
        async with httpx.AsyncClient(base_url=url.replace("ws://", "http://"), trust_env=False) as client:
            paired = await client.post("/api/devices/pairing")
            self.assertEqual(paired.status_code, 200)
            code = paired.json()["pairing_code"]
        saved = root / "executor-credential.json"
        script = Path(__file__).resolve().parents[1] / "examples" / "device_executor.py"
        environment = dict(os.environ)
        environment["XAGENT_PAIRING_CODE"] = code
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(script), "--api-url", url, "--device-id", "desk",
            "--credentials-file", str(saved), env=environment,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        async def invoke(**args):
            messages = []
            await host.agent.tool_executor.handle_tool_calls(
                [chat_test.FakeToolCall("device", json.dumps(args))], messages, user_id="alice", channel="cli",
            )
            return json.loads(messages[-1]["content"])
        try:
            await self.wait_for(lambda: "desk" in host.device_service.devices)
            await self.wait_for(saved.exists)
            credential = json.loads(saved.read_text())["credential"]
            self.assertNotIn(credential, (root / ".devices.json").read_text())
            self.assertNotIn("credentials", (root / "config.yaml").read_text())
            listed = await invoke(action="list", location="office")
            self.assertEqual({cap["name"] for cap in listed["devices"][0]["capabilities"]}, {"counter.set", "counter.read"})
            result = await invoke(action="call", device_id="desk", capability="counter.set", params={"value": 41})
            self.assertEqual(result["status"], "success")
            rejected = await invoke(action="call", device_id="desk", capability="counter.set", params={"value": True})
            self.assertEqual(rejected["code"], "device_error")
            await host.disconnect_channel("api")
            self.assertTrue(host.device_service.resources_closed)
            await host.connect_channel("api")
            await self.wait_for(lambda: host.channel_status["api"]["state"] == "connected")
            await self.wait_for(lambda: "desk" in host.device_service.devices)
            read = await invoke(action="call", device_id="desk", capability="counter.read")
            self.assertEqual(read["data"], {"value": 41})
            self.assertFalse(host.device_service.pending)
            await RuntimeClient(root).stop(restart=True)
            await asyncio.wait_for(host_task, timeout=5)
            host, host_task = await runtime_test.RuntimeTransportTests._start_host(self, root, chat_test.CapturingModelClient([]))
            self.hosts.append((host, host_task))
            await self.wait_for(lambda: host.channel_status["api"]["state"] == "connected")
            await self.wait_for(lambda: "desk" in host.device_service.devices)
            self.assertIsNone(process.returncode)
            read = await invoke(action="call", device_id="desk", capability="counter.read")
            self.assertEqual(read["data"], {"value": 41})
            process.terminate()
            await asyncio.wait_for(process.communicate(), timeout=3)
            await self.wait_for(lambda: not host.device_service.devices)
            environment.pop("XAGENT_PAIRING_CODE")
            process = await asyncio.create_subprocess_exec(
                sys.executable, str(script), "--api-url", url, "--device-id", "desk",
                "--credentials-file", str(saved), env=environment,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            await self.wait_for(lambda: "desk" in host.device_service.devices)
            read = await invoke(action="call", device_id="desk", capability="counter.read")
            self.assertEqual(read["data"], {"value": 0})
        finally:
            if process.returncode is None:
                process.terminate()
            await asyncio.wait_for(process.communicate(), timeout=3)

    async def test_example_executor_streams_a_configured_photo(self):
        host, _, root, url = await self.start_host([], seed_credentials=False)
        code = host.device_service.create_pairing()["pairing_code"]
        photo = root / "capture.png"
        photo.write_bytes(media_test.picture())
        saved = root / "media-executor-credential.json"
        script = Path(__file__).resolve().parents[1] / "examples" / "device_executor.py"
        environment = dict(os.environ)
        environment["XAGENT_PAIRING_CODE"] = code
        process = await asyncio.create_subprocess_exec(sys.executable, str(script), "--api-url", url,
            "--device-id", "desk", "--credentials-file", str(saved), "--capture-file", str(photo),
            env=environment, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            await self.wait_for(lambda: "desk" in host.device_service.devices)
            result = await host.agent.tools["device"](action="call", device_id="desk", capability="camera.capture", params={}, timeout_seconds=90)
            self.assertEqual(result["status"], "success")
            attachment, = result["attachments"]
            self.assertEqual((root / "workspace" / attachment["path"]).read_bytes(), photo.read_bytes())
            self.assertEqual((await host.agent.tools["see_image"](path=attachment["path"]))["status"], "ok")
        finally:
            if process.returncode is None:
                process.terminate()
            await asyncio.wait_for(process.communicate(), 5)

    async def test_pairing_needs_no_config_and_all_callers_can_use_devices(self):
        host, _, root, url = await self.start_host([], seed_credentials=False)
        service = host.device_service
        initial_config = (root / "config.yaml").read_text()
        self.assertNotIn("devices", yaml.safe_load(initial_config))
        self.assertIn("device", host.agent.tools)
        async with httpx.AsyncClient(base_url=url.replace("ws://", "http://"), trust_env=False) as client:
            created = await client.post("/api/devices/pairing")
            self.assertEqual(created.status_code, 200)
            code = created.json()["pairing_code"]
            async with websockets.connect(url + "/ws/devices/new-id", additional_headers={"Authorization": "Bearer " + code}, **self.connection_options) as device:
                await device.send(json.dumps(device_test.manifest("new-id").model_dump()))
                credential = json.loads(await device.recv())["credential"]
                await self.wait_for(lambda: "new-id" in service.devices)
                for user_id, channel, room in (("alice", "cli", None), ("new-user", "api", None), ("another", "feishu", "group"), ("", "api", None)):
                    messages = []
                    attribution = {"user_id": user_id, "channel": channel, "room_name": room}
                    await host.agent.tool_executor.handle_tool_calls(
                        [chat_test.FakeToolCall("device", '{"action":"list"}')], messages, **attribution,
                    )
                    self.assertEqual(json.loads(messages[-1]["content"])["devices"][0]["device_id"], "new-id")
                    pending = asyncio.create_task(host.agent.tool_executor.handle_tool_calls([
                        chat_test.FakeToolCall("device", '{"action":"call","device_id":"new-id","capability":"custom.execute"}'),
                    ], messages, **attribution))
                    command = json.loads(await asyncio.wait_for(device.recv(), timeout=3))
                    await device.send(json.dumps({"type": "result", "command_id": command["command_id"], "ok": True, "data": user_id}))
                    await pending
                    self.assertEqual(json.loads(messages[-1]["content"])["status"], "success")
                self.assertEqual((await client.delete("/api/devices/new-id")).status_code, 200)
                self.assertFalse(service.devices)
                self.assertFalse(service.authenticate("new-id", "Bearer " + credential))
            self.assertEqual((root / "config.yaml").read_text(), initial_config)
            self.assertIs(host.device_service, service)
            self.assertFalse(host.status()["needs_restart"])

    async def test_web_admin_uses_owner_runtime_when_public_api_is_disconnected(self):
        from xagent.interfaces.web.server import WebClientServer
        from xagent.interfaces.cli.agents import register_agent
        host, _, root, url = await self.start_host([], seed_credentials=False)
        register_agent("test_agent", path=root, make_active=True, root=root / "registry")
        server = WebClientServer(host="127.0.0.1", port=1415, api_url=url.replace("ws://", "http://"), config_dir=str(root), initial_agent="test_agent", registry_root=root / "registry")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://web") as client:
            state = (await client.get("/api/devices")).json()
            self.assertTrue(state["runtime_available"])
            self.assertTrue(state["api_available"])
            created = await client.post("/api/devices/pairing")
            self.assertEqual(created.status_code, 200)
            self.assertEqual(created.headers["cache-control"], "no-store")
            await host.disconnect_channel("api")
            state = (await client.get("/api/devices")).json()
            self.assertFalse(state["api_available"])
            self.assertTrue(state["runtime_available"])
            self.assertEqual((await client.post("/api/devices/pairing")).status_code, 409)

    async def test_quiescing_api_closes_new_device_handshake_as_temporarily_unavailable(self):
        host, _, _, _ = await self.start_host([])
        admission = host._servers["api"].config.app
        host._channel_activity["api"].quiesce()
        messages = []
        async def send(message):
            messages.append(message)
        async def receive():
            self.fail("Quiescing API must not read new device registrations")
        await admission({"type": "websocket", "path": "/ws/devices/desk", "extensions": {"websocket.http.response": {}}}, receive, send)
        self.assertEqual(messages, [{"type": "websocket.accept"}, {"type": "websocket.close", "code": 1013}])
        messages.clear()
        await admission({"type": "websocket", "path": "/ws/chat", "extensions": {"websocket.http.response": {}}}, receive, send)
        self.assertEqual(messages, [{"type": "websocket.close", "code": 1013}])

    async def test_runtime_restart_requires_registration_and_never_replays_old_command(self):
        host, host_task, root, url = await self.start_host([])
        old_service = host.device_service
        async with self.device_socket(url) as device:
            await self.register(device, host)
            pending = asyncio.create_task(old_service.call(CallRequest(device_id="desk", capability="custom.execute")))
            old_command = json.loads(await asyncio.wait_for(device.recv(), timeout=3))
            await RuntimeClient(root).stop(restart=True)
            await asyncio.wait_for(host_task, timeout=5)
            self.assertEqual((await pending)["status"], "unknown")
            self.assertTrue(old_service.resources_closed)

        replacement, task = await runtime_test.RuntimeTransportTests._start_host(self, root, chat_test.CapturingModelClient([]))
        self.hosts.append((replacement, task))
        await self.wait_for(lambda: replacement.channel_status["api"]["state"] == "connected")
        self.assertIsNot(replacement.device_service, old_service)
        self.assertEqual(replacement.device_service.list_devices()["devices"], [])
        self.assertFalse(replacement.device_service.pending)
        async with self.device_socket(url) as device:
            await self.register(device, replacement)
            await device.send(json.dumps({"type": "result", "command_id": old_command["command_id"], "ok": True}))
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(device.recv(), timeout=0.05)
            pending = asyncio.create_task(replacement.device_service.call(CallRequest(device_id="desk", capability="custom.execute")))
            command = json.loads(await asyncio.wait_for(device.recv(), timeout=3))
            self.assertNotEqual(command["command_id"], old_command["command_id"])
            await device.send(json.dumps({"type": "result", "command_id": command["command_id"], "ok": True}))
            self.assertEqual((await pending)["status"], "success")
            self.assertFalse(replacement.device_service.pending)
