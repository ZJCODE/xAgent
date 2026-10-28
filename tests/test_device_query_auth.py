"""Browser WebSockets authenticate device sockets with ?token= as well as Authorization."""

from __future__ import annotations

import time
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from xagent.integrations.api import ApiChannelAdapter
from xagent.integrations.devices.service import DeviceService
from xagent.interfaces.server import AgentHTTPServer
from xagent.interfaces.server.device_routes import resolve_device_credential
import tests.test_device_execution as device_test


class ResolveDeviceCredentialTests(unittest.TestCase):
    def test_query_token_becomes_bearer_and_header_wins(self):
        self.assertEqual(resolve_device_credential(token="dvc_abc"), "Bearer dvc_abc")
        self.assertEqual(resolve_device_credential(token="pair_abc"), "Bearer pair_abc")
        self.assertEqual(resolve_device_credential(token="  bearer   dvc_abc  "), "Bearer dvc_abc")
        self.assertEqual(
            resolve_device_credential(authorization="Bearer secret", token="dvc_other"),
            "Bearer secret",
        )
        self.assertEqual(resolve_device_credential(authorization="Bearer secret", token=""), "Bearer secret")
        self.assertEqual(resolve_device_credential(authorization="   ", token="dvc_abc"), "Bearer dvc_abc")
        self.assertEqual(resolve_device_credential(token=""), "")
        self.assertEqual(resolve_device_credential(token="   "), "")
        self.assertEqual(resolve_device_credential(token="Bearer"), "Bearer Bearer")
        self.assertEqual(resolve_device_credential(token="has space"), "")
        self.assertEqual(resolve_device_credential(token="x" * 513), "")

    def test_header_value_is_preserved_exactly(self):
        self.assertEqual(resolve_device_credential(authorization="Bearer  spaced"), "Bearer  spaced")
        self.assertEqual(resolve_device_credential(authorization="bearer lower"), "bearer lower")


class QueryTokenProtocolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.service = DeviceService(device_test.device_config(), root / ".devices.json")
        self.service.start()
        agent = device_test.ToolChatAgent(self.service)
        adapter = ApiChannelAdapter(
            agent,
            contacts_file=root / "contacts.json",
            tasks_dir=root / "tasks",
            device_service=self.service,
        )
        server = AgentHTTPServer(config_dir=str(root), agent=agent, api_adapter=adapter, runtime_managed=True)
        self.client = TestClient(server.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.addCleanup(lambda: self.client.portal.call(self.service.stop))

    def pairing_code(self):
        response = self.client.post("/api/devices/pairing")
        self.assertEqual(response.status_code, 200)
        return response.json()["pairing_code"]

    def _wait_online(self, device_id):
        deadline = time.monotonic() + 1
        state = {}
        while time.monotonic() < deadline:
            state = self.client.get("/api/devices").json()
            row = next((item for item in state["devices"] if item["device_id"] == device_id), None)
            if row and row.get("online"):
                return row
            time.sleep(0.01)
        self.fail(f"{device_id} did not come online: {state}")

    def test_control_socket_accepts_query_token_and_header_still_authenticates(self):
        code = self.pairing_code()
        with self.client.websocket_connect("/ws/devices/desk?token=" + quote(code, safe="")) as device:
            device.send_json(device_test.manifest().model_dump())
            paired = device.receive_json()
        self.assertEqual(paired["type"], "paired")
        credential = paired["credential"]
        self.assertTrue(credential.startswith("dvc_"))

        with self.client.websocket_connect(
            "/ws/devices/desk",
            headers={"Authorization": "Bearer " + credential},
        ) as device:
            device.send_json(device_test.manifest(capability="header.still").model_dump())
            row = self._wait_online("desk")
        self.assertEqual(row["capabilities"][0]["name"], "header.still")

        with self.assertRaises(WebSocketDisconnect) as missing:
            with self.client.websocket_connect("/ws/devices/desk"):
                pass
        self.assertEqual(missing.exception.code, 1008)
        with self.assertRaises(WebSocketDisconnect) as rejected:
            with self.client.websocket_connect("/ws/devices/desk?token=not-a-credential"):
                pass
        self.assertEqual(rejected.exception.code, 1008)
        with self.assertRaises(WebSocketDisconnect) as header_wins:
            with self.client.websocket_connect(
                "/ws/devices/desk?token=" + quote(credential, safe=""),
                headers={"Authorization": "Bearer wrong-secret"},
            ):
                pass
        self.assertEqual(header_wins.exception.code, 1008)

        with self.client.websocket_connect(
            "/ws/devices/desk?token=not-a-credential",
            headers={"Authorization": "Bearer " + credential},
        ) as device:
            device.send_json(device_test.manifest().model_dump())
            self._wait_online("desk")

        self.service.credentials.add("cam", "sec/ret+x")
        with self.client.websocket_connect("/ws/devices/cam?token=" + quote("sec/ret+x", safe="")) as device:
            device.send_json(device_test.manifest("cam").model_dump())
            self._wait_online("cam")

    def _voice(self, device_id, *, token=None, header=None):
        path = "/ws/devices/" + device_id + "/voice"
        headers = {}
        if token is not None:
            path += "?token=" + quote(token, safe="")
        if header is not None:
            headers["Authorization"] = header if header.startswith("Bearer ") else "Bearer " + header
        return self.client.websocket_connect(path, headers=headers)

    def test_voice_socket_rejects_bad_query_token_before_the_header_fallback(self):
        with self.assertRaises(WebSocketDisconnect) as rejected:
            with self._voice("phone-web", token="not-a-credential"):
                pass
        self.assertEqual(rejected.exception.code, 1008)

        code = self.pairing_code()
        with self.assertRaises(WebSocketDisconnect) as header_wins:
            with self._voice("phone-web", token=code, header="Bearer wrong-secret"):
                pass
        self.assertEqual(header_wins.exception.code, 1008)
        self.assertTrue(self.service.authenticate("phone-web", "Bearer " + code))
        self.assertNotIn("phone-web", self.service.credentials.digests)
