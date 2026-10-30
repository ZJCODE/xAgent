"""WS voice replaces host capture without reviving old startup or routing paths."""
import argparse
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from tests.test_agent_edit_setup import _write_agent
from tests.test_voice_runtime import FakeAgent
from xagent.core.runtime.host import RuntimeHost
from xagent.integrations.devices.service import DeviceService
from xagent.interfaces.cli.agent_runtime import handle_channel_connection
from xagent.interfaces.cli.channels import ChannelSelectionError, normalize_channel_values, restart_channels_from_status
from xagent.interfaces.voice.config import VoiceChannelConfig
from xagent.interfaces.voice.factory import create_websocket_voice_runtime
from xagent.interfaces.voice.runtime import VoiceRuntimeOptions
from xagent.interfaces.web.server import WebClientServer


class VoiceWSMigrationTests(unittest.TestCase):
    def test_removed_channels_do_not_start_and_old_restart_snapshots_skip_host(self):
        with self.assertRaisesRegex(ChannelSelectionError, "Host voice has been removed"):
            normalize_channel_values(["api,voice"], default="api")
        with patch("sys.stdout", new_callable=io.StringIO) as output, patch(
            "xagent.interfaces.cli.agent_runtime.RuntimeClient"
        ) as client:
            self.assertEqual(handle_channel_connection(argparse.Namespace(channel="voice")), 1)
            client.assert_not_called()
        self.assertIn("WebSocket", output.getvalue())
        self.assertEqual(restart_channels_from_status({"channels": {
            "api": {"state": "connected"}, "voice": {"state": "connected"},
        }}), ["api"])

    def test_removed_routes_explain_migration_and_no_host_devices_are_exposed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = _write_agent(root)
            server = WebClientServer(host="127.0.0.1", port=1416, api_url="http://127.0.0.1:8010",
                                     config_dir=str(directory), registry_root=root)
            client = TestClient(server.app)
            for method, path in (("post", "connect"), ("post", "disconnect"),
                                 ("post", "setup"), ("get", "setup-schema"), ("get", "logs")):
                response = getattr(client, method)(f"/api/channels/voice/{path}", **({"json": {}} if method == "post" else {}))
                self.assertEqual(response.status_code, 410, response.text)
            self.assertNotIn("/api/agent/voice/devices", server.app.openapi()["paths"])
            response = client.patch("/api/agent/voice", json={"audio_input": "Mic"})
            self.assertEqual(response.status_code, 422)

    def test_legacy_audio_is_ignored_even_if_the_host_selection_is_invalid(self):
        config = VoiceChannelConfig.from_dict({"api_key": "key", "audio": "retired", "enabled": True})
        self.assertEqual(config.to_public_dict(), {"api_key": "key", "voice": "Daniel", "interruptions": False})

    def test_delivery_requires_matching_target_and_never_falls_back_to_host(self):
        session = SimpleNamespace(device_id="desk", room_name="Office",
                                  runtime=SimpleNamespace(options=SimpleNamespace(user_id="alice")))
        service = SimpleNamespace(voice_sessions={"desk": session}, resolve_room_name=lambda device_id: "Office")
        resolve = lambda target=None, user_id="": DeviceService.resolve_voice_session(service, target, user_id=user_id)
        self.assertIs(resolve({"device_id": "desk"}), session)
        self.assertIsNone(resolve({"device_id": "offline", "room_name": "Office"}))
        self.assertIsNone(resolve(user_id="device:offline"))
        self.assertIsNone(resolve(user_id="bob"))
        self.assertIsNone(resolve())
        self.assertIs(resolve(user_id="alice"), session)
        service.voice_sessions["other"] = SimpleNamespace(device_id="other", room_name="Office", runtime=session.runtime)
        self.assertIsNone(resolve({"room_name": "Office"}))
        self.assertIsNone(resolve(user_id="alice"))
        with tempfile.TemporaryDirectory() as temporary:
            host = RuntimeHost(temporary)
            host.adapters["voice"] = object()  # Even stale in-memory host state is never a destination.
            self.assertIsNone(host._resolve_voice_adapter({}))
            with self.assertRaises(HTTPException) as error:
                host._require_channel_control("voice")
            self.assertEqual(error.exception.status_code, 410)

    def test_ws_factory_applies_shared_preferences_and_session_policy_before_adapters(self):
        seen = []
        def adapters(config, **kwargs):
            seen.append((config.profile, config.enable_diarization, config.voice, config.speed,
                         config.enable_interruptions, config.runtime_profile.echo_managed))
            return object(), object()
        for direct, profile in ((True, "headset"), (False, "room")):
            bridge = SimpleNamespace(hello=SimpleNamespace(profile=profile, echo_managed=True, interruptions=True),
                                     room_name="Test room", recognition_unavailable=lambda: None)
            with patch("xagent.interfaces.voice.factory.create_soniox_adapters", side_effect=adapters), patch.multiple(
                "xagent.interfaces.voice.factory", WebSocketMicrophone=lambda *a, **k: object(),
                WebSocketPlayer=lambda *a, **k: object(), VoiceRuntime=lambda **k: object(),
                make_websocket_output_sink=lambda *a: object(),
            ):
                create_websocket_voice_runtime(agent=FakeAgent(),
                    config=VoiceChannelConfig.from_dict({"api_key": "key", "voice": "Maya", "speed": 1.25}),
                    bridge=bridge, options=VoiceRuntimeOptions(direct_conversation=direct))
        self.assertEqual(seen, [("headset", False, "Maya", 1.25, True, True),
                                ("room", True, "Maya", 1.25, True, True)])
