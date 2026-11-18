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

    def test_legacy_store_loads_with_reusable_pairing_disabled(self):
        self.path.write_text(json.dumps({"version": 1, "devices": {
            "desk": DeviceCredentials._digest("desk", "saved-secret"),
        }}))
        credentials = DeviceCredentials(self.path)
        self.assertIsNone(credentials.reusable_pairing_digest)
        self.assertTrue(credentials.authenticate("desk", "saved-secret"))
        self.assertFalse(credentials.authenticate_reusable_pairing("saved-secret"))

    def test_reusable_code_enrolls_multiple_devices_and_survives_restart(self):
        self.assertIsNone(self.service.credentials.reusable_pairing_digest)
        code = self.service.create_reusable_pairing()["pairing_code"]
        tokens = [self.service.complete_pairing(device_test.manifest(identity), "Bearer " + code)
                  for identity in ("desk", "robot")]
        self.assertNotEqual(*tokens)
        self.service.quiesce()
        self.assertFalse(self.service.authenticate("new", "Bearer " + code))
        replacement = DeviceService(self.service.config, self.path)
        replacement.start()
        with patch("xagent.integrations.devices.service.time.monotonic", return_value=time.monotonic() + 86400):
            self.assertTrue(replacement.authenticate("new", "Bearer " + code))
        for identity, token in zip(("desk", "robot"), tokens):
            self.assertTrue(replacement.authenticate(identity, "Bearer " + token))
            self.assertFalse(replacement.authenticate(identity, "Bearer " + code))
        stored = self.path.read_text()
        for secret in [code, *tokens]:
            self.assertNotIn(secret, stored)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_reusable_reset_disable_and_device_changes_preserve_other_credentials(self):
        old = self.service.create_reusable_pairing()["pairing_code"]
        token = self.service.complete_pairing(device_test.manifest(), "Bearer " + old)
        new = self.service.create_reusable_pairing()["pairing_code"]
        self.assertFalse(self.service.authenticate("other", "Bearer " + old))
        self.assertTrue(self.service.authenticate("other", "Bearer " + new))
        self.assertTrue(self.service.authenticate("desk", "Bearer " + token))
        with self.assertRaises(ValueError):
            self.service.complete_pairing(device_test.manifest(), "Bearer " + new)
        self.service.credentials.add("other", "other-secret")
        self.service.credentials.revoke("other")
        self.assertTrue(DeviceCredentials(self.path).authenticate_reusable_pairing(new))
        self.service.disable_reusable_pairing()
        self.assertFalse(self.service.authenticate("other", "Bearer " + new))
        self.assertTrue(self.service.authenticate("desk", "Bearer " + token))
        self.assertIsNone(DeviceCredentials(self.path).reusable_pairing_digest)

    def test_reusable_and_single_use_codes_are_independent(self):
        temporary = self.service.create_pairing()["pairing_code"]
        reusable = self.service.create_reusable_pairing()["pairing_code"]
        self.service.complete_pairing(device_test.manifest(), "Bearer " + reusable)
        self.assertTrue(self.service.authenticate("robot", "Bearer " + temporary))
        self.service.complete_pairing(device_test.manifest("robot"), "Bearer " + temporary)
        self.assertFalse(self.service.authenticate("third", "Bearer " + temporary))
        self.service.create_pairing()
        self.service.cancel_pairing()
        self.assertTrue(self.service.authenticate("third", "Bearer " + reusable))
        temporary = self.service.create_pairing()["pairing_code"]
        self.service.disable_reusable_pairing()
        self.assertTrue(self.service.authenticate("third", "Bearer " + temporary))

    def test_reusable_code_supports_voice_pairing_but_not_media_or_other_agents(self):
        code = self.service.create_reusable_pairing()["pairing_code"]
        token = self.service.complete_pairing_for_voice("speaker", "Bearer " + code, name="Speaker")
        self.assertTrue(self.service.authenticate("speaker", "Bearer " + token))
        self.assertFalse(self.service.authenticate_media("speaker", "Bearer " + code))
        self.assertTrue(self.service.authenticate_media("speaker", "Bearer " + token))
        self.assertTrue(self.service.authenticate("robot", "Bearer " + code))
        with self.assertRaises(ValueError):
            self.service.complete_pairing_for_voice("speaker", "Bearer " + code)
        other = DeviceService(self.service.config)
        other.start()
        self.assertFalse(other.authenticate("robot", "Bearer " + code))

    def test_reusable_write_failure_preserves_previous_code_in_memory_and_on_disk(self):
        code = self.service.create_reusable_pairing()["pairing_code"]
        before = self.path.read_bytes()
        with patch("xagent.integrations.devices.credentials.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.service.create_reusable_pairing()
            with self.assertRaises(OSError):
                self.service.disable_reusable_pairing()
        self.assertTrue(self.service.authenticate("desk", "Bearer " + code))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

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

    def test_browser_voice_ticket_is_temporary_single_use_and_voice_only(self):
        code = self.service.create_pairing()["pairing_code"]
        session = self.service.create_browser_voice_session()
        identity, credential = session["session_id"], "Bearer " + session["token"]
        self.assertFalse(self.service.authenticate(identity, credential))
        self.assertFalse(self.service.authenticate_media(identity, credential))
        self.assertFalse(self.service.consume_browser_voice_ticket("other", credential))
        self.assertFalse(self.service.consume_browser_voice_ticket(identity, "Bearer wrong"))
        self.assertTrue(self.service.consume_browser_voice_ticket(identity, credential))
        self.assertFalse(self.service.consume_browser_voice_ticket(identity, credential))
        self.assertTrue(self.service.authenticate("desk", "Bearer " + code))
        self.assertFalse(self.service.credentials.digests)
        self.assertFalse(self.path.exists())

    def test_browser_voice_ticket_expires_and_is_invalidated_on_shutdown(self):
        session = self.service.create_browser_voice_session()
        deadline = self.service._browser_voice_tickets[session["session_id"]][1]
        with patch("xagent.integrations.devices.service.time.monotonic", return_value=deadline):
            self.assertFalse(self.service.consume_browser_voice_ticket(session["session_id"], "Bearer " + session["token"]))
        session = self.service.create_browser_voice_session()
        self.service.quiesce()
        self.service.start()
        self.assertFalse(self.service.consume_browser_voice_ticket(session["session_id"], "Bearer " + session["token"]))

    def test_pending_browser_voice_tickets_are_bounded_and_expired_tickets_are_pruned(self):
        with patch("xagent.integrations.devices.service.MAX_BROWSER_VOICE_TICKETS", 2):
            self.service.create_browser_voice_session()
            self.service.create_browser_voice_session()
            with self.assertRaises(RuntimeError):
                self.service.create_browser_voice_session()
            with patch("xagent.integrations.devices.service.time.monotonic", return_value=time.monotonic() + 61):
                self.service.create_browser_voice_session()
                self.assertEqual(len(self.service._browser_voice_tickets), 1)


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

    def test_browser_voice_tickets_require_trusted_admin_origin_and_are_not_cached(self):
        response = self.client.post("/api/devices/voice-session")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertTrue(response.json()["token"].startswith("voice_"))
        self.assertEqual(self.client.get("/api/devices").json()["devices"], [])
        denied = self.client.post("/api/devices/voice-session", headers={"Origin": "https://untrusted.example"})
        self.assertEqual(denied.status_code, 403)

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

    def test_reusable_admin_and_multiple_device_handshakes(self):
        self.assertFalse(self.client.get("/api/devices").json()["reusable_pairing_enabled"])
        response = self.client.post("/api/devices/pairing/reusable")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        code = response.json()["pairing_code"]
        with self.socket("desk", code) as first, self.client.websocket_connect("/ws/devices/robot?token=" + code) as second:
            first.send_json(device_test.manifest().model_dump())
            first_token = first.receive_json()["credential"]
            second.send_json(device_test.manifest("robot").model_dump())
            second_token = second.receive_json()["credential"]
            self.assertNotEqual(first_token, second_token)
            state = self.client.get("/api/devices").json()
            self.assertTrue(state["reusable_pairing_enabled"])
            for secret in (code, first_token, second_token):
                self.assertNotIn(secret, json.dumps(state))
            new = self.client.post("/api/devices/pairing/reusable").json()["pairing_code"]
            self.assertFalse(self.service.authenticate("third", "Bearer " + code))
            self.assertTrue(self.service.authenticate("third", "Bearer " + new))
            self.assertEqual(self.client.delete("/api/devices/pairing/reusable").status_code, 200)
            self.assertFalse(self.client.get("/api/devices").json()["reusable_pairing_enabled"])
            self.assertTrue(self.service.authenticate("desk", "Bearer " + first_token))
            self.assertTrue(self.service.authenticate("robot", "Bearer " + second_token))
            self.assertFalse(self.service.authenticate("third", "Bearer " + new))

    def test_reusable_management_requires_trusted_origin_and_reports_save_failure(self):
        for method in (self.client.post, self.client.delete):
            self.assertEqual(method("/api/devices/pairing/reusable", headers={"Origin": "https://untrusted.example"}).status_code, 403)
        with patch("xagent.integrations.devices.credentials.os.replace", side_effect=OSError("disk full")):
            self.assertEqual(self.client.post("/api/devices/pairing/reusable").status_code, 500)
        self.assertFalse(self.client.get("/api/devices").json()["reusable_pairing_enabled"])
        self.client.post("/api/devices/pairing/reusable")
        with patch("xagent.integrations.devices.credentials.os.replace", side_effect=OSError("disk full")):
            self.assertEqual(self.client.delete("/api/devices/pairing/reusable").status_code, 500)
        self.assertTrue(self.client.get("/api/devices").json()["reusable_pairing_enabled"])
        self.service.quiesce()
        self.assertEqual(self.client.post("/api/devices/pairing/reusable").status_code, 409)
        self.assertEqual(self.client.delete("/api/devices/pairing/reusable").status_code, 200)

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
        self.service.create_reusable_pairing()
        with patch("xagent.interfaces.base.DeviceService", side_effect=AssertionError("Offline service created")):
            admin = AdminService(config_dir=str(self.root))
            app = FastAPI()
            register_device_admin_routes(app, lambda: admin)
            with TestClient(app) as client:
                state = client.get("/api/devices").json()
                self.assertFalse(state["runtime_available"])
                self.assertTrue(state["reusable_pairing_enabled"])
                self.assertEqual(state["devices"], [{"device_id": "desk", "online": False}])
                self.assertNotIn("secret", json.dumps(state))
                self.assertEqual(client.post("/api/devices/pairing").status_code, 503)
                self.assertEqual(client.post("/api/devices/pairing/reusable").status_code, 503)
                self.assertEqual(client.delete("/api/devices/pairing/reusable").status_code, 503)
