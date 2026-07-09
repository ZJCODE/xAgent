"""Tests for web-client runtime channel management routes."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import yaml

from xagent.interfaces.cli.agents import register_agent
from xagent.interfaces.cli.processes import StartResult
from xagent.interfaces.clients.web.server import WebClientServer


def _write_agent(path: Path, *, port: int, include_integrations: bool = True) -> None:
    path.mkdir(parents=True, exist_ok=True)
    channels = {
        "api": {"host": "127.0.0.1", "port": port},
    }
    if include_integrations:
        channels.update({
            "voice": {"enabled": True, "provider": "soniox"},
            "feishu": {"app_id": "cli_test", "app_secret": "secret"},
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
        with patch("xagent.interfaces.clients.web.channel_routes.running_pid", return_value=None):
            async with await self._client() as client:
                response = await client.get("/api/channels")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        rows = {row["id"]: row for row in payload["channels"]}
        self.assertEqual(rows["api"]["status"], "stopped")
        self.assertTrue(rows["api"]["ready"])
        self.assertEqual(rows["voice"]["status"], "disabled")
        self.assertEqual(rows["feishu"]["setup_hint"], "")
        self.assertEqual(rows["weixin"]["setup_hint"], "")

    async def test_start_uses_selected_agent_config_dir(self):
        self.server.session.select("agent_b")
        with patch(
            "xagent.interfaces.clients.web.channel_routes.running_pid",
            side_effect=[None, 4321],
        ), patch(
            "xagent.interfaces.clients.web.channel_routes.start_background",
            return_value=StartResult(ok=True, pid=4321),
        ) as start:
            async with await self._client() as client:
                response = await client.post("/api/channels/api/start")

        self.assertEqual(response.status_code, 200)
        args, kwargs = start.call_args
        self.assertIn(str(self.agent_b_path.resolve()), args[0])
        self.assertEqual(kwargs["pid_path"], self.agent_b_path.resolve() / "run" / "api.pid")
        self.assertEqual(kwargs["log_path"], self.agent_b_path.resolve() / "logs" / "api.log")

    async def test_stop_uses_selected_agent_pid_path(self):
        self.server.session.select("agent_b")
        with patch(
            "xagent.interfaces.clients.web.channel_routes.running_pid",
            return_value=None,
        ), patch(
            "xagent.interfaces.clients.web.channel_routes.stop_managed_process",
            return_value=(True, "stopped (pid=4321)"),
        ) as stop:
            async with await self._client() as client:
                response = await client.post("/api/channels/voice/stop")

        self.assertEqual(response.status_code, 200)
        stop.assert_called_once_with(self.agent_b_path.resolve() / "run" / "voice.pid")

    async def test_restart_stops_then_starts_selected_channel(self):
        self.server.session.select("agent_b")
        with patch(
            "xagent.interfaces.clients.web.channel_routes.running_pid",
            side_effect=[1111, 2222],
        ), patch(
            "xagent.interfaces.clients.web.channel_routes.stop_managed_process",
            return_value=(True, "stopped (pid=1111)"),
        ) as stop, patch(
            "xagent.interfaces.clients.web.channel_routes.start_background",
            return_value=StartResult(ok=True, pid=2222),
        ) as start:
            async with await self._client() as client:
                response = await client.post("/api/channels/feishu/restart")

        self.assertEqual(response.status_code, 200)
        stop.assert_called_once_with(self.agent_b_path.resolve() / "run" / "feishu.pid")
        self.assertEqual(start.call_args.kwargs["pid_path"], self.agent_b_path.resolve() / "run" / "feishu.pid")

    async def test_voice_setup_schema_reports_unconfigured_state(self):
        async with await self._client() as client:
            response = await client.get("/api/channels/voice/setup-schema")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["configured"])
        self.assertIn("soniox", {row["id"] for row in payload["voice_providers"]})

    async def test_voice_setup_writes_config_for_selected_agent(self):
        async with await self._client() as client:
            response = await client.post(
                "/api/channels/voice/setup",
                json={
                    "force": False,
                    "selection": {
                        "voice_provider": "soniox",
                        "voice_api_key": "voice-test-key",
                        "voice_wake_enabled": False,
                        "voice_enable_interruptions": False,
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["channel"]["ready"])

        config = yaml.safe_load((self.agent_a_path / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(config["channels"]["voice"]["provider"], "soniox")

    async def test_feishu_manual_setup_writes_credentials(self):
        async with await self._client() as client:
            response = await client.post(
                "/api/channels/feishu/setup",
                json={
                    "force": False,
                    "selection": {
                        "credential_mode": "manual",
                        "app_id": "web_app",
                        "app_secret": "web_secret",
                        "stream": True,
                        "group_fetch_limit": 5,
                        "group_reply_only_when_mentioned": True,
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        config = yaml.safe_load((self.agent_a_path / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(config["channels"]["feishu"]["app_id"], "web_app")
        self.assertEqual(config["channels"]["feishu"]["app_secret"], "web_secret")
        self.assertIs(config["channels"]["feishu"]["stream"], True)

    async def test_voice_setup_conflict_without_force_returns_409(self):
        async with await self._client() as client:
            first = await client.post(
                "/api/channels/voice/setup",
                json={"force": False, "selection": {"voice_provider": "soniox"}},
            )
            second = await client.post(
                "/api/channels/voice/setup",
                json={"force": False, "selection": {"voice_provider": "qwen"}},
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
                        "voice_provider": "soniox",
                        "voice_api_key": "voice-test-key",
                    },
                },
            )

            updated = await client.get("/api/agent/config")
            self.assertEqual(updated.status_code, 200)
            self.assertIn("voice:", updated.json()["config"])

    async def test_start_channel_qr_returns_session_payload(self):
        with patch(
            "xagent.interfaces.clients.web.qr_sessions.ChannelQrSessionManager.start_feishu",
        ) as start_feishu:
            from xagent.interfaces.clients.web.qr_sessions import ChannelQrSession

            start_feishu.return_value = ChannelQrSession(
                id="sess_test",
                channel="feishu",
                status="pending",
            )
            async with await self._client() as client:
                response = await client.post("/api/channels/feishu/qr/start")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["session_id"], "sess_test")

    async def test_cancel_channel_qr_marks_session_cancelled(self):
        from xagent.interfaces.clients.web.qr_sessions import get_qr_session_manager

        manager = get_qr_session_manager()
        with patch.object(manager, "_run_feishu_registration", lambda session, cancel_event: None):
            session = manager.start_feishu()
        async with await self._client() as client:
            response = await client.delete(f"/api/channels/feishu/qr/{session.id}")
            self.assertEqual(response.status_code, 200)
            poll = await client.get(f"/api/channels/feishu/qr/{session.id}")
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
