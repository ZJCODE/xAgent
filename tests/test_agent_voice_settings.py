"""Shared voice settings stay in Agent and apply to every audio transport."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import yaml
from fastapi.testclient import TestClient

from tests.test_agent_edit_setup import _write_agent
from tests.test_voice_runtime import FakeAgent
from xagent.interfaces.cli.config_editor import apply_agent_edit_setup, load_config
from xagent.interfaces.cli.setup import build_voice_setup_schema
from xagent.interfaces.voice.config import VoiceChannelConfig
from xagent.interfaces.voice.factory import create_local_voice_runtime, create_websocket_voice_runtime
from xagent.interfaces.voice.runtime import VoiceRuntimeOptions
from xagent.interfaces.web.server import WebClientServer


class AgentVoiceSettingsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.agent_dir = _write_agent(self.root)
        server = WebClientServer(host="127.0.0.1", port=1416, api_url="http://127.0.0.1:8010",
                                 config_dir=str(self.agent_dir), registry_root=self.root)
        self.server = server
        self.client = TestClient(server.app)

    def test_readiness_needs_no_audio_hardware_and_does_not_expose_keys(self):
        with patch.dict("os.environ", {"SONIOX_API_KEY": ""}), patch(
            "xagent.interfaces.voice.audio.list_audio_device_options", side_effect=AssertionError("No host inventory needed")):
            settings = self.client.get("/api/agent/voice").json()
            self.assertFalse(settings["configured"])
            self.assertFalse(settings["has_api_key"])
            self.assertFalse(settings["restart_required"])
            self.assertEqual(set(settings), {"configured", "has_api_key", "voice", "speed", "interruptions",
                                             "audio_input", "audio_output", "voice_options", "restart_required"})
            response = self.client.patch("/api/agent/voice", json={"voice_api_key": "secret-soniox-key", "voice": "Maya"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("secret-soniox-key", response.text)
            state = self.client.get("/api/agent/voice")
            self.assertTrue(state.json()["configured"])
            self.assertTrue(state.json()["has_api_key"])
            self.assertEqual(state.json()["voice"], "Maya")
            self.assertFalse(response.json()["restart_required"])
            self.assertNotIn("secret-soniox-key", state.text)
        config = load_config(self.agent_dir)
        self.assertEqual(config["channels"]["voice"]["voice"], "Maya")
        self.assertNotIn("speed", config["channels"]["voice"])
        self.assertFalse((self.agent_dir / ".devices.json").exists())

    def test_changing_shared_voice_preserves_key_host_devices_and_other_connections(self):
        config = load_config(self.agent_dir)
        config["channels"]["voice"] = {"api_key": "keep-key", "voice": "Daniel", "interruptions": True,
                                          "speed": 1.25, "enabled": False,
                                          "audio": {"input": "Desk mic", "output": "Desk speaker"}}
        (self.agent_dir / "config.yaml").write_text(yaml.safe_dump(config))
        response = self.client.patch("/api/agent/voice", json={"voice_api_key": "", "voice": "Maya"})
        self.assertEqual(response.status_code, 200, response.text)
        changed = load_config(self.agent_dir)
        expected = {**config["channels"]["voice"], "voice": "Maya"}
        self.assertEqual(changed["channels"]["voice"], expected)
        self.assertEqual(changed["channels"]["api"], config["channels"]["api"])
        schema = build_voice_setup_schema(changed)
        self.assertEqual(schema["defaults"]["voice_api_key"], "")
        self.assertEqual(schema["defaults"]["speed"], 1.25)

    def test_voice_endpoint_rejects_unrelated_parameters(self):
        original = (self.agent_dir / "config.yaml").read_text()
        for field, value in (("enabled", True), ("provider", "other")):
            with self.subTest(field=field):
                response = self.client.patch("/api/agent/voice", json={"voice_api_key": "key", field: value})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual((self.agent_dir / "config.yaml").read_text(), original)

    def test_preferences_round_trip_and_reset_without_losing_the_saved_key(self):
        response = self.client.patch("/api/agent/voice", json={
            "voice_api_key": "keep-key", "voice": "Maya", "speed": 1.25, "interruptions": True,
            "audio_input": "Desk mic", "audio_output": 3,
        })
        self.assertEqual(response.status_code, 200, response.text)
        state = self.client.get("/api/agent/voice").json()
        self.assertEqual((state["voice"], state["speed"], state["interruptions"]), ("Maya", 1.25, True))
        self.assertEqual((state["audio_input"], state["audio_output"]), ("Desk mic", 3))
        response = self.client.patch("/api/agent/voice", json={
            "speed": 1, "interruptions": False, "audio_input": "auto", "audio_output": "auto",
        })
        self.assertEqual(response.status_code, 200, response.text)
        saved = load_config(self.agent_dir)["channels"]["voice"]
        self.assertEqual(saved, {"api_key": "keep-key", "voice": "Maya", "interruptions": False})

    def test_device_inventory_is_fetched_separately_from_voice_readiness(self):
        options = {"input": [{"id": "auto"}, {"id": "1", "label": "USB mic"}], "output": [{"id": "auto"}]}
        with patch("xagent.interfaces.voice.audio.list_audio_device_options", return_value=options) as inventory:
            self.client.get("/api/agent/voice")
            inventory.assert_not_called()
            response = self.client.get("/api/agent/voice/devices")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), options)
            self.assertEqual(response.headers["cache-control"], "no-store")
            inventory.assert_called_once()

    def test_invalid_preference_types_leave_the_file_unchanged(self):
        original = (self.agent_dir / "config.yaml").read_text()
        for values in ({"speed": True}, {"speed": 0.1}, {"interruptions": "false"}, {"audio_input": True}):
            with self.subTest(values=values):
                response = self.client.patch("/api/agent/voice", json={"voice_api_key": "key", **values})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual((self.agent_dir / "config.yaml").read_text(), original)

    def test_restart_is_needed_only_for_changed_settings_on_the_runtime_owner(self):
        self.server.session.get_current_admin().is_runtime_owner = True
        changed = self.client.patch("/api/agent/voice", json={"voice_api_key": "key", "voice": "Maya"})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertTrue(changed.json()["restart_required"])
        unchanged = self.client.patch("/api/agent/voice", json={"voice": "Maya"})
        self.assertFalse(unchanged.json()["changed"])
        self.assertFalse(unchanged.json()["restart_required"])

    def test_invalid_speed_is_rejected_before_persisting(self):
        initial = (self.agent_dir / "config.yaml").read_text()
        for speed in (True, 0.1, 3, float("inf"), float("nan")):
            with self.subTest(speed=speed), self.assertRaises(ValueError):
                apply_agent_edit_setup(self.agent_dir, "voice", {"voice_api_key": "new-key", "speed": speed})
            self.assertEqual((self.agent_dir / "config.yaml").read_text(), initial)

    def test_saved_settings_explain_when_the_running_agent_needs_restart(self):
        with patch("xagent.interfaces.cli.agent_runtime.runtime_status", new_callable=AsyncMock,
                   return_value={"runtime_running": True, "needs_restart": True}):
            state = self.client.get("/api/agent/voice").json()
        self.assertTrue(state["restart_required"])

    def test_environment_credentials_work_without_copying_them_to_config(self):
        with patch.dict("os.environ", {"SONIOX_API_KEY": "environment-key"}):
            self.assertTrue(self.client.get("/api/agent/voice").json()["configured"])
            response = self.client.post("/api/agent/setup/voice", json={"voice": "Maya", "speed": 1.25})
            self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("environment-key", (self.agent_dir / "config.yaml").read_text())

    def test_host_and_websocket_audio_use_the_same_saved_voice_and_speed(self):
        agent = FakeAgent()
        agent.voice = "Daniel"  # A stale Agent attribute must not override the shared settings.
        hello = SimpleNamespace(profile="headset", echo_managed=True, interruptions=None)
        bridge = SimpleNamespace(hello=hello, room_name="Browser voice")
        audio = SimpleNamespace(topology=SimpleNamespace(near_field=True, echo_managed=True, reason="test"),
                                input_selection=SimpleNamespace(device_index=0, device_name="Mic", stream_sample_rate=16000, stream_channels=1),
                                output_selection=SimpleNamespace(device_index=1, device_name="Speaker", stream_sample_rate=24000, stream_channels=1))
        with patch("xagent.interfaces.voice.factory.create_soniox_adapters", return_value=(object(), object())) as adapters, patch(
            "xagent.interfaces.voice.factory.resolve_audio_io_profile", return_value=audio), patch.multiple(
                "xagent.interfaces.voice.factory", SoundDeviceMicrophone=object_mock(), SoundDevicePlayer=object_mock(),
                WebSocketMicrophone=object_mock(), WebSocketPlayer=object_mock(), VoiceRuntime=object_mock(),
                make_websocket_output_sink=object_mock()):
            settings = {"api_key": "key", "voice": "Maya", "speed": 1.25}
            create_local_voice_runtime(agent=agent, config=VoiceChannelConfig.from_dict(settings), options=VoiceRuntimeOptions())
            create_websocket_voice_runtime(agent=agent, config=VoiceChannelConfig.from_dict(settings),
                                           bridge=bridge, options=VoiceRuntimeOptions())
        self.assertEqual([(call.args[0].voice, call.args[0].speed) for call in adapters.call_args_list],
                         [("Maya", 1.25), ("Maya", 1.25)])


def object_mock():
    return lambda *args, **kwargs: object()
