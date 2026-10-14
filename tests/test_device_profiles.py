"""User labels survive device defaults, disconnects and runtime restarts."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from xagent.integrations.devices.profiles import DeviceProfiles
from xagent.integrations.devices.service import DeviceService
from xagent.interfaces.server.device_admin import register_device_admin_routes
from tests.test_device_execution import FakeSocket, device_config, manifest


class DeviceProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.service = DeviceService(device_config(), self.root / ".devices.json")
        self.service.credentials.add("desk", "secret")
        self.service.start()
        self.server = SimpleNamespace(device_service=self.service, config_dir=self.root, config={})
        app = FastAPI()
        register_device_admin_routes(app, lambda: self.server)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def register(self, **overrides):
        return asyncio.run(self.service.register(FakeSocket(), manifest(**overrides)))

    def edit(self, **fields):
        return self.client.patch("/api/devices/desk", json=fields)

    def row(self):
        return self.client.get("/api/devices").json()["devices"][0]

    def test_edit_survives_reconnect_restart_and_changes_agent_discovery(self):
        self.register(name="XIAO ESP32-S3 Sense")
        response = self.edit(name=" 桌面相机 ", location="书房", description="镜头朝向桌面上的纸张")
        self.assertEqual(response.status_code, 200, response.text)
        self.register(name="Firmware default", location="office", capability="camera.capture")
        row = self.row()
        self.assertEqual(row["name"], "桌面相机")
        self.assertEqual(row["location"], "书房")
        self.assertEqual(row["description"], "镜头朝向桌面上的纸张")
        self.assertEqual(row["reported_name"], "Firmware default")
        self.assertEqual(row["capabilities"][0]["name"], "camera.capture")
        self.assertEqual(self.service.list_devices(location="office")["devices"], [])
        self.assertEqual(self.service.list_devices(location="书房")["devices"][0]["name"], "桌面相机")
        self.assertNotIn("secret", json.dumps(row))
        replacement = DeviceService(device_config(), self.root / ".devices.json")
        self.server.device_service = replacement
        self.assertFalse(self.row()["online"])
        self.assertEqual(self.row()["description"], row["description"])
        self.server.device_service = None
        self.assertEqual(self.row()["name"], "桌面相机")
        # Basic metadata can be edited without a running Agent or online hardware.
        self.assertEqual(self.edit(location="随身").status_code, 200)
        self.assertEqual(self.row()["location"], "随身")

    def test_partial_updates_empty_values_and_reset_to_device_defaults(self):
        self.register(name="Default")
        self.edit(name="Custom", location="Study", description="Notes")
        self.assertEqual(self.edit(description="").status_code, 200)
        self.assertEqual(self.row()["name"], "Custom")
        self.assertEqual(self.row()["description"], "")
        self.edit(location="")
        self.assertEqual(self.row()["location"], "")
        self.edit(name=None, location=None)
        self.assertEqual(self.row()["name"], "Default")
        self.assertEqual(self.row()["location"], "office")

    def test_only_valid_profile_fields_can_be_edited(self):
        for fields in ({"name": 123}, {"name": "x" * 257}, {"description": "x" * 2049},
                       {"capabilities": []}, {"device_id": "other"}, {"metadata": {}}):
            self.assertEqual(self.client.patch("/api/devices/desk", json=fields).status_code, 422)
        self.assertEqual(self.client.patch("/api/devices/missing", json={"name": "Custom"}).status_code, 404)
        self.assertEqual(self.client.patch("/api/devices/desk", json={"name": "Custom"},
                         headers={"Origin": "https://untrusted.example"}).status_code, 403)

    def test_failed_save_keeps_previous_profile_in_memory_and_on_disk(self):
        self.edit(name="Original")
        before = (self.root / ".device_profiles.json").read_bytes()
        with patch("xagent.integrations.devices.profiles.os.replace", side_effect=OSError("disk full")):
            self.assertEqual(self.edit(name="Lost").status_code, 500)
        self.assertEqual(self.row()["name"], "Original")
        self.assertEqual((self.root / ".device_profiles.json").read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), [".device_profiles.json", ".devices.json"])

    def test_profiles_are_agent_local_and_new_pairing_does_not_inherit_old_labels(self):
        self.register()
        self.edit(name="Old label")
        self.assertEqual(DeviceProfiles().fields("desk", self.service.credentials.digests["desk"]), {})
        self.assertEqual(self.client.delete("/api/devices/desk").status_code, 200)
        self.assertEqual(self.client.get("/api/devices").json()["devices"], [])
        self.service.credentials.add("desk", "new-secret")
        self.register(name="New device")
        self.assertEqual(self.row()["name"], "New device")
        self.assertEqual(self.row()["user_profile"], {})
