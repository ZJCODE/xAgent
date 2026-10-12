"""One-time enrollment, credential continuity and trusted administration."""

import json
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient
from pydantic import ValidationError

from xagent.integrations.api import ApiChannelAdapter
from xagent.integrations.devices.credentials import DeviceCredentials
from xagent.integrations.devices.service import DeviceService, CallRequest
from xagent.interfaces.server import AgentHTTPServer
from xagent.interfaces.server.admin_service import AdminService
from xagent.interfaces.server.device_admin import register_device_admin_routes
import tests.test_device_execution as device_test


class PairingCredentialsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / ".devices.json"
        self.service = DeviceService(device_test.device_config(), self.path)
        self.service.start()

    def test_pairing_binds_any_new_id_and_survives_restart_without_plaintext_secrets(self):
        registration = device_test.manifest("new-device", capability="anything.run")
        code = self.service.create_pairing()["pairing_code"]
        issued = self.service.complete_pairing(registration, "Bearer " + code)
        self.assertTrue(self.service.authenticate("new-device", "Bearer " + issued))
        self.assertFalse(self.service.authenticate("another", "Bearer " + issued))
        self.assertFalse(self.service.authenticate("another", "Bearer " + code))
        replacement = DeviceService(self.service.config, self.path)
        self.assertTrue(replacement.authenticate("new-device", "Bearer " + issued))
        self.assertTrue(replacement.resources_closed)
        stored = self.path.read_text()
        self.assertNotIn(issued, stored)
        self.assertNotIn(code, stored)
        self.assertNotIn("capabilities", stored)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_expiry_replacement_and_cancellation_reject_codes(self):
        first = self.service.create_pairing()["pairing_code"]
        second = self.service.create_pairing()["pairing_code"]
        self.assertFalse(self.service.authenticate("desk", "Bearer " + first))
        self.assertTrue(self.service.authenticate("desk", "Bearer " + second))
        with patch("xagent.integrations.devices.service.time.monotonic", return_value=self.service._pairing_deadline):
            self.assertFalse(self.service.authenticate("desk", "Bearer " + second))
        self.service.cancel_pairing()
        self.assertFalse(self.service.authenticate("desk", "Bearer " + second))

    def test_existing_identity_cannot_be_taken_over_by_new_pairing(self):
        self.service.credentials.add("desk", "original-secret")
        code = self.service.create_pairing()["pairing_code"]
        self.assertFalse(self.service.authenticate("desk", "Bearer " + code))
        with self.assertRaises(ValueError):
            self.service.complete_pairing(device_test.manifest(), "Bearer " + code)
        self.assertTrue(self.service.authenticate("desk", "Bearer original-secret"))
        self.assertTrue(self.service.authenticate("other", "Bearer " + code))

    def test_invalid_manifest_and_failed_persistence_do_not_consume_pairing(self):
        code = self.service.create_pairing()["pairing_code"]
        invalid = device_test.manifest(capabilities=[{"name": "duplicate"}, {"name": "duplicate"}])
        with self.assertRaises(ValueError):
            self.service.complete_pairing(invalid, "Bearer " + code)
        with patch("xagent.integrations.devices.credentials.os.replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.service.complete_pairing(device_test.manifest(), "Bearer " + code)
        self.assertFalse(self.service.credentials.digests)
        self.assertTrue(self.service.authenticate("desk", "Bearer " + code))
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_disconnected_service_cannot_pair_and_stop_invalidates_codes(self):
        code = self.service.create_pairing()["pairing_code"]
        self.service.quiesce()
        self.assertFalse(self.service.authenticate("desk", "Bearer " + code))
        with self.assertRaises(RuntimeError):
            self.service.create_pairing()
        self.service.start()
        self.assertTrue(self.service.create_pairing()["pairing_code"])

    def test_corrupted_credential_file_fails_closed(self):
        self.path.write_text('{"version":1,"devices":{"desk":"not-a-digest"}}')
        with self.assertRaises(ValidationError):
            DeviceCredentials(self.path)
        self.assertIn("not-a-digest", self.path.read_text())


class PairingProtocolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.service = DeviceService(device_test.device_config(), self.root / ".devices.json")
        self.service.start()
        agent = device_test.ToolChatAgent(self.service)
        adapter = ApiChannelAdapter(agent, contacts_file=self.root / "contacts.json", tasks_dir=self.root / "tasks", device_service=self.service)
        server = AgentHTTPServer(config_dir=str(self.root), agent=agent, api_adapter=adapter, runtime_managed=True)
        self.client = TestClient(server.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.addCleanup(lambda: self.client.portal.call(self.service.stop))

    def socket(self, device_id, token):
        return self.client.websocket_connect("/ws/devices/" + device_id, headers={"Authorization": "Bearer " + token})

    def pairing_code(self):
        response = self.client.post("/api/devices/pairing")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()["pairing_code"]

    def test_pairing_uses_same_device_socket_and_existing_protocol_on_reconnect(self):
        code = self.pairing_code()
        with self.socket("desk", code) as device:
            device.send_json(device_test.manifest().model_dump())
            paired = device.receive_json()
            self.assertEqual(paired["type"], "paired")
            self.assertEqual(paired["device_id"], "desk")
        with self.socket("desk", paired["credential"]) as device:
            device.send_json(device_test.manifest(capability="brand.new").model_dump())
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                state = self.client.get("/api/devices").json()
                if state["devices"][0]["online"] and state["devices"][0].get("capabilities", [{}])[0].get("name") == "brand.new":
                    break
                time.sleep(0.005)
            self.assertEqual(state["devices"][0]["capabilities"][0]["name"], "brand.new")
            self.assertNotIn(paired["credential"], json.dumps(state))
            self.assertNotIn(code, json.dumps(state))

    def test_two_connections_cannot_both_consume_one_pairing_code(self):
        code = self.pairing_code()
        with self.socket("desk", code) as first, self.socket("robot", code) as second:
            first.send_json(device_test.manifest().model_dump())
            self.assertEqual(first.receive_json()["type"], "paired")
            second.send_json(device_test.manifest("robot").model_dump())
            with self.assertRaises(WebSocketDisconnect) as caught:
                second.receive_json()
            self.assertEqual(caught.exception.code, 1008)
        self.assertEqual(list(self.service.credentials.digests), ["desk"])

    def test_invalid_registration_does_not_pair_or_burn_the_code(self):
        code = self.pairing_code()
        with self.socket("desk", code) as device:
            device.send_json(device_test.manifest("other").model_dump())
            with self.assertRaises(WebSocketDisconnect):
                device.receive_json()
        self.assertFalse(self.service.credentials.digests)
        self.assertTrue(self.service.authenticate("desk", "Bearer " + code))

    def test_revocation_closes_connection_and_rejects_old_credential(self):
        code = self.pairing_code()
        with self.socket("desk", code) as device:
            device.send_json(device_test.manifest().model_dump())
            token = device.receive_json()["credential"]
            call = self.client.portal.start_task_soon(self.service.call, CallRequest(device_id="desk", capability="custom.execute"))
            command = device.receive_json()
            self.assertEqual(command["type"], "command")
            self.assertEqual(self.client.delete("/api/devices/desk").status_code, 200)
            self.assertEqual(call.result(timeout=1)["status"], "unknown")
            self.assertFalse(self.service.pending)
            with self.assertRaises(WebSocketDisconnect):
                device.receive_json()
        with self.assertRaises(WebSocketDisconnect):
            with self.socket("desk", token):
                pass
        self.assertFalse(DeviceCredentials(self.root / ".devices.json").digests)
        self.assertFalse(self.service.devices)

    def test_cross_origin_management_rejected_and_cancelled_codes_fail(self):
        denied = self.client.post("/api/devices/pairing", headers={"Origin": "https://untrusted.example"})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.client.get("/api/devices", headers={"Origin": "null"}).status_code, 403)
        self.assertEqual(self.client.post("/api/devices/pairing", headers={"Origin": "http://["}).status_code, 403)
        code = self.pairing_code()
        self.assertEqual(self.client.delete("/api/devices/pairing").status_code, 200)
        with self.assertRaises(WebSocketDisconnect):
            with self.socket("desk", code):
                pass

    def test_offline_admin_only_reads_configuration_and_paired_ids(self):
        self.service.credentials.add("desk", "secret")
        with patch("xagent.interfaces.base.DeviceService", side_effect=AssertionError("Offline service created")):
            admin = AdminService(config_dir=str(self.root))
            app = FastAPI()
            register_device_admin_routes(app, lambda: admin)
            with TestClient(app) as client:
                state = client.get("/api/devices").json()
                self.assertFalse(state["runtime_available"])
                self.assertEqual(state["devices"], [{"device_id": "desk", "online": False}])
                self.assertNotIn("secret", json.dumps(state))
                self.assertEqual(client.post("/api/devices/pairing").status_code, 503)
