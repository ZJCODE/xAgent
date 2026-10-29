"""Tests for web-client runtime channel management routes."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import yaml

from xagent.interfaces.cli.agents import register_agent
from xagent.interfaces.web.server import WebClientServer


def _write_agent(path: Path, *, port: int, include_integrations: bool = True) -> None:
    path.mkdir(parents=True, exist_ok=True)
    channels = {
        "api": {"host": "127.0.0.1", "port": port},
    }
    if include_integrations:
        channels.update({
            "voice": {"api_key": "soniox-key"},
            "weixin": {"account_id": "wx_test"},
        })
    config = {
        "provider": {
            "name": "openai",
            "api_key": "test-key",
            "model": "gpt-5.4-mini",
        },
        "channels": channels,
    }
    (path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (path / "identity.md").write_text("# Identity\n\nTest agent.\n", encoding="utf-8")


class WebChannelRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.agent_a_path = self.root / "agents" / "agent_a"
        self.agent_b_path = self.root / "agents" / "agent_b"
        _write_agent(self.agent_a_path, port=8010, include_integrations=False)
        _write_agent(self.agent_b_path, port=9010)
        register_agent("agent_a", path=self.agent_a_path, make_active=True, root=self.root)
        register_agent("agent_b", path=self.agent_b_path, root=self.root)
        self.server = WebClientServer(
            host="127.0.0.1",
            port=1415,
            api_url="http://127.0.0.1:8010",
            config_dir=str(self.agent_a_path),
            initial_agent="agent_a",
            registry_root=self.root,
        )

    async def _client(self):
        transport = httpx.ASGITransport(app=self.server.app)
        return httpx.AsyncClient(transport=transport, base_url="http://testserver")

    async def test_list_channels_reports_unconfigured_integrations(self):
        with patch("xagent.interfaces.web.channel_routes.running_pid", return_value=None), patch(
            "xagent.interfaces.cli.agent_runtime.runtime_status", new_callable=AsyncMock,
            return_value={"status": "stopped", "runtime_running": False, "channels": {}},
        ):
            async with await self._client() as client:
                response = await client.get("/api/channels")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        rows = {row["id"]: row for row in payload["channels"]}
        self.assertEqual(rows["api"]["status"], "disconnected")
        self.assertTrue(rows["api"]["ready"])
        self.assertEqual(rows["voice"]["status"], "disabled")
        self.assertEqual(rows["weixin"]["setup_hint"], "")
        for row in rows.values():
            self.assertFalse(row["can_start"])
            self.assertFalse(row["can_stop"])
            self.assertFalse(row["can_restart"])
        self.assertFalse(payload["runtime"]["runtime_running"])

    async def test_start_runtime_uses_selected_agent_config_dir(self):
        self.server.session.select("agent_b")
        with patch(
            "xagent.interfaces.cli.agent_runtime.start_runtime",
            return_value={"status": "running", "runtime_running": True, "pid": 4321},
        ) as start:
            async with await self._client() as client:
                response = await client.post("/api/runtime/start")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["pid"], 4321)
        start.assert_called_once_with(self.agent_b_path.resolve())

    async def test_stop_runtime_uses_selected_agent_config_dir(self):
        self.server.session.select("agent_b")
        with patch(
            "xagent.interfaces.cli.agent_runtime.stop_runtime", new_callable=AsyncMock,
            return_value={"status": "stopped", "runtime_running": False},
        ) as stop:
            async with await self._client() as client:
                response = await client.post("/api/runtime/stop")

        self.assertEqual(response.status_code, 200)
        stop.assert_awaited_once_with(self.agent_b_path.resolve())
        self.assertFalse(response.json()["runtime_running"])

    async def test_restart_uses_shared_flow_for_selected_runtime_and_returns_warnings(self):
        self.server.session.select("agent_b")
        with patch(
            "xagent.interfaces.cli.agent_runtime.restart_runtime", new_callable=AsyncMock,
            return_value={"status": "running", "pid": 2222, "restart_warnings": ["voice: disabled"]},
        ) as restart:
            async with await self._client() as client:
                response = await client.post("/api/runtime/restart")

        self.assertEqual(response.status_code, 200)
        restart.assert_awaited_once_with(self.agent_b_path.resolve())
        self.assertEqual(response.json()["restart_warnings"], ["voice: disabled"])

    async def test_restart_does_not_start_after_runtime_stop_failure(self):
        with patch("xagent.interfaces.cli.agent_runtime.stop_runtime", new_callable=AsyncMock,
                   side_effect=RuntimeError("owner has not released runtime")), patch(
            "xagent.interfaces.cli.agent_runtime.start_runtime",
        ) as start:
            async with await self._client() as client:
                response = await client.post("/api/runtime/restart")
        self.assertEqual(response.status_code, 503)
        self.assertIn("owner has not released", response.json()["detail"])
        start.assert_not_called()

    async def test_channel_lifecycle_routes_are_unavailable(self):
        with patch("xagent.interfaces.cli.agent_runtime.start_runtime") as start, patch(
            "xagent.interfaces.cli.agent_runtime.stop_runtime", new_callable=AsyncMock,
        ) as stop:
            async with await self._client() as client:
                for channel in ("api", "voice", "weixin"):
                    for action in ("start", "stop", "restart"):
                        response = await client.post(f"/api/channels/{channel}/{action}")
                        self.assertEqual(response.status_code, 404)
        start.assert_not_called()
        stop.assert_not_awaited()

    async def test_connection_actions_target_selected_agent(self):
        self.server.session.select("agent_b")
        runtime = {"runtime_ready": True, "runtime_running": True, "status": "running", "channels": {}}
        with patch("xagent.interfaces.cli.agent_runtime.runtime_status", new_callable=AsyncMock, return_value=runtime), patch(
            "xagent.core.runtime.client.RuntimeClient.connect_channel", new_callable=AsyncMock
        ) as connect, patch("xagent.core.runtime.client.RuntimeClient.disconnect_channel", new_callable=AsyncMock) as disconnect, patch(
            "xagent.core.runtime.client.RuntimeClient.__init__", return_value=None
        ) as client_init:
            async with await self._client() as client:
                first = await client.post("/api/channels/weixin/connect")
                second = await client.post("/api/channels/weixin/disconnect")
            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 200)
            self.assertEqual(first.json()["channel"]["id"], "weixin")
            connect.assert_awaited_once_with("weixin")
            disconnect.assert_awaited_once_with("weixin")
            self.assertTrue(all(call.args == (self.agent_b_path.resolve(),) for call in client_init.call_args_list))

    async def test_connection_requires_ready_runtime_and_valid_channel(self):
        with patch("xagent.interfaces.cli.agent_runtime.runtime_status", new_callable=AsyncMock,
                   return_value={"runtime_ready": False}), patch(
            "xagent.core.runtime.client.RuntimeClient.connect_channel", new_callable=AsyncMock
        ) as connect:
            async with await self._client() as client:
                self.assertEqual((await client.post("/api/channels/weixin/connect")).status_code, 409)
                self.assertEqual((await client.post("/api/channels/invalid/connect")).status_code, 404)
            connect.assert_not_awaited()

    async def test_connection_response_keeps_original_agent_when_selection_changes(self):
        self.server.session.select("agent_b")
        async def connect(channel):
            self.server.session.select("agent_a")
        with patch("xagent.interfaces.cli.agent_runtime.runtime_status", new_callable=AsyncMock,
                   return_value={"runtime_ready": True, "channels": {}}), patch(
            "xagent.core.runtime.client.RuntimeClient.connect_channel", side_effect=connect
        ):
            async with await self._client() as client:
                result = await client.post("/api/channels/weixin/connect")
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()["channel"]["configured"])
        self.assertEqual(result.json()["channel"]["log_path"], str(self.agent_b_path.resolve() / "logs" / "runtime.log"))

    async def test_connection_error_preserves_runtime_validation_status(self):
        error = httpx.HTTPStatusError("invalid", request=httpx.Request("POST", "http://local"),
            response=httpx.Response(422, json={"detail": "Configure weixin before connecting"}))
        with patch("xagent.interfaces.cli.agent_runtime.runtime_status", new_callable=AsyncMock,
                   return_value={"runtime_ready": True}), patch(
            "xagent.core.runtime.client.RuntimeClient.connect_channel", new_callable=AsyncMock, side_effect=error
        ):
            async with await self._client() as client:
                result = await client.post("/api/channels/weixin/connect")
            self.assertEqual(result.status_code, 422)
            self.assertEqual(result.json()["detail"], "Configure weixin before connecting")

    async def test_connections_follow_snapshot_not_shared_pid_or_saved_config(self):
        self.server.session.select("agent_b")
        runtime = {"runtime_ready": True, "runtime_running": True, "status": "running",
                   "channels": {"weixin": {"state": "disconnected", "ready": True,
                                             "can_connect": True, "can_disconnect": False},
                                "voice": {"state": "disconnected", "ready": False,
                                          "config_error": "Restart to apply configuration",
                                          "can_connect": False, "can_disconnect": False}}}
        with patch("xagent.interfaces.cli.agent_runtime.runtime_status", new_callable=AsyncMock, return_value=runtime), patch(
            "xagent.interfaces.web.channel_routes.running_pid", return_value=123
        ):
            async with await self._client() as client:
                result = await client.get("/api/channels")
        rows = {row["id"]: row for row in result.json()["channels"]}
        self.assertEqual(rows["weixin"]["status"], "disconnected")
        self.assertTrue(rows["weixin"]["can_connect"])
        self.assertFalse(rows["voice"]["ready"])
        self.assertFalse(rows["voice"]["can_connect"])

    async def test_channels_and_runtime_report_degraded_state_and_task_review_count(self):
        runtime = {"status": "degraded", "runtime_running": True, "runtime_ready": True,
                   "channels": {"api": {"status": "running"},
                                "weixin": {"status": "failed", "error": "connection lost"}},
                   "tasks": {"pending": 2, "failed": 1, "needs_review": 3}}
        with patch("xagent.interfaces.cli.agent_runtime.runtime_status", new_callable=AsyncMock,
                   return_value=runtime) as status:
            async with await self._client() as client:
                response = await client.get("/api/channels")
                runtime_response = await client.get("/api/runtime")
        rows = {row["id"]: row for row in response.json()["channels"]}
        self.assertEqual(rows["api"]["status"], "running")
        self.assertEqual(rows["weixin"]["status"], "error")
        self.assertEqual(rows["weixin"]["detail"], "connection lost")
        self.assertEqual(response.json()["runtime"]["tasks"]["needs_review"], 3)
        self.assertEqual(runtime_response.json(), runtime)
        self.assertEqual(status.await_count, 2)

    async def test_channel_log_view_uses_selected_runtime_log(self):
        self.server.session.select("agent_b")
        with patch("xagent.interfaces.web.channel_routes.tail_text", return_value="Agent startup complete") as tail:
            async with await self._client() as client:
                runtime_response = await client.get("/api/runtime/logs?lines=15")
                channel_response = await client.get("/api/channels/voice/logs?lines=15")
        expected_path = self.agent_b_path.resolve() / "logs" / "runtime.log"
        self.assertEqual(runtime_response.json()["log_path"], str(expected_path))
        self.assertEqual(channel_response.json()["log_path"], str(expected_path))
        self.assertEqual(channel_response.json()["text"], "Agent startup complete")
        self.assertEqual(tail.call_args.args, (expected_path,))
        self.assertEqual(tail.call_args.kwargs, {"max_lines": 15})

    async def test_voice_setup_schema_reports_unconfigured_state(self):
        async with await self._client() as client:
            response = await client.get("/api/channels/voice/setup-schema")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["configured"])
        self.assertEqual(payload["defaults"]["voice_api_key"], "")
        self.assertNotIn("languages", payload["defaults"])
        self.assertNotIn("voice_providers", payload)
        self.assertNotIn("qwen_voice_api_key", payload["placeholders"])

    async def test_voice_setup_writes_config_for_selected_agent(self):
        async with await self._client() as client:
            response = await client.post(
                "/api/channels/voice/setup",
                json={
                    "force": False,
                    "selection": {
                        "voice_api_key": "voice-test-key",
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["channel"]["ready"])

        config = yaml.safe_load((self.agent_a_path / "config.yaml").read_text(encoding="utf-8"))
        voice_cfg = config["channels"]["voice"]
        self.assertEqual(voice_cfg["api_key"], "voice-test-key")
        self.assertNotIn("profile", voice_cfg)

    async def test_voice_setup_edit_preserves_flat_advanced_options(self):
        config_path = self.agent_a_path / "config.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["channels"]["voice"] = {
            "api_key": "old-key",
            "languages": ["en", "zh"],
            "audio": {"input": "Mic", "output": "Speaker"},
        }
        config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

        async with await self._client() as client:
            response = await client.post(
                "/api/channels/voice/setup",
                json={
                    "force": True,
                    "selection": {"voice_api_key": "new-key"},
                },
            )

        self.assertEqual(response.status_code, 200)
        saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))["channels"]["voice"]
        self.assertEqual(saved["api_key"], "new-key")
        self.assertNotIn("languages", saved)
        self.assertEqual(saved["audio"]["input"], "Mic")

    async def test_voice_setup_always_configures_channel(self):
        config_path = self.agent_a_path / "config.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["channels"]["voice"] = {"api_key": "old-key"}
        config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

        async with await self._client() as client:
            response = await client.post(
                "/api/channels/voice/setup",
                json={"force": True, "selection": {"voice_api_key": "new-key"}},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["channel"]["configured"])
        saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["channels"]["voice"]["api_key"], "new-key")

    async def test_voice_settings_round_trip_and_reset_defaults(self):
        self.server.session.select("agent_b")
        config_path = self.agent_b_path / "config.yaml"
        original = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        async with await self._client() as client:
            for selection in (
                {"voice": "Maya", "interruptions": True, "audio_input": "Test microphone", "audio_output": "Test speaker"},
                {"voice": "Daniel", "interruptions": False, "audio_input": "auto", "audio_output": "auto"},
            ):
                with self.subTest(selection=selection):
                    response = await client.post(
                        "/api/channels/voice/setup",
                        json={"force": True, "selection": {"voice_api_key": "", "speed": 1.0, **selection}},
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.json()["channel"]["ready"])
                    schema_response = await client.get("/api/channels/voice/setup-schema")
                    self.assertEqual(schema_response.status_code, 200)
                    schema = schema_response.json()
                    self.assertTrue(schema["configured"])
                    self.assertEqual(schema["defaults"], {"voice_api_key": "", "speed": 1.0, **selection})
                    self.assertEqual(
                        [option["id"] for option in schema["voice_options"]],
                        ["Maya", "Daniel", "Adrian", "Claire", "Oliver", "Isla"],
                    )
                    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                    self.assertEqual(saved["channels"]["voice"]["api_key"], "soniox-key")
                    for channel in ("api", "weixin"):
                        self.assertEqual(saved["channels"][channel], original["channels"][channel])
                    self.assertEqual(saved["provider"], original["provider"])
                    other_agent = yaml.safe_load((self.agent_a_path / "config.yaml").read_text(encoding="utf-8"))
                    self.assertNotIn("voice", other_agent["channels"])

    async def test_voice_setup_conflict_without_force_returns_409(self):
        async with await self._client() as client:
            first = await client.post(
                "/api/channels/voice/setup",
                json={"force": False, "selection": {"voice_api_key": "one"}},
            )
            second = await client.post(
                "/api/channels/voice/setup",
                json={"force": False, "selection": {"voice_api_key": "two"}},
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)

    async def test_voice_setup_invalidates_cached_admin_config(self):
        async with await self._client() as client:
            initial = await client.get("/api/agent/config")
            self.assertEqual(initial.status_code, 200)
            self.assertNotIn("voice:", initial.json()["config"])

            await client.post(
                "/api/channels/voice/setup",
                json={
                    "force": False,
                    "selection": {
                        "voice_api_key": "voice-test-key",
                    },
                },
            )

            updated = await client.get("/api/agent/config")
            self.assertEqual(updated.status_code, 200)
            self.assertIn("voice:", updated.json()["config"])

    async def test_start_channel_qr_returns_session_payload(self):
        with patch(
            "xagent.interfaces.web.qr_sessions.ChannelQrSessionManager.start_weixin",
        ) as start_weixin:
            from xagent.interfaces.web.qr_sessions import ChannelQrSession

            start_weixin.return_value = ChannelQrSession(
                id="sess_test",
                channel="weixin",
                status="pending",
            )
            async with await self._client() as client:
                response = await client.post("/api/channels/weixin/qr/start")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["session_id"], "sess_test")

    async def test_cancel_channel_qr_marks_session_cancelled(self):
        from xagent.interfaces.web.qr_sessions import get_qr_session_manager

        manager = get_qr_session_manager()
        with patch.object(manager, "_run_weixin_login", lambda session, cancel_event, config_dir, options: None):
            session = manager.start_weixin(config_dir=self.root)
        async with await self._client() as client:
            response = await client.delete(f"/api/channels/weixin/qr/{session.id}")
            self.assertEqual(response.status_code, 200)
            poll = await client.get(f"/api/channels/weixin/qr/{session.id}")
        self.assertEqual(poll.status_code, 200)
        self.assertEqual(poll.json()["status"], "cancelled")

    async def test_start_weixin_qr_populates_qr_url(self):
        import asyncio
        import time
        from unittest.mock import patch

        async def fake_qr_login(**kwargs):
            render = kwargs.get("render_qr_url")
            if render is not None:
                render("https://liteapp.weixin.qq.com/q/test?qrcode=abc")
            await asyncio.sleep(3600)

        with patch(
            "xagent.integrations.weixin.client.qr_login",
            side_effect=fake_qr_login,
        ):
            async with await self._client() as client:
                response = await client.post("/api/channels/weixin/qr/start")
                self.assertEqual(response.status_code, 200)
                session_id = response.json()["session_id"]

                deadline = time.monotonic() + 3.0
                qr_url = None
                while time.monotonic() < deadline:
                    poll = await client.get(f"/api/channels/weixin/qr/{session_id}")
                    self.assertEqual(poll.status_code, 200)
                    body = poll.json()
                    if body.get("qr_url"):
                        qr_url = body["qr_url"]
                        break
                    await asyncio.sleep(0.1)

                await client.delete(f"/api/channels/weixin/qr/{session_id}")

        self.assertEqual(qr_url, "https://liteapp.weixin.qq.com/q/test?qrcode=abc")


if __name__ == "__main__":
    unittest.main()
