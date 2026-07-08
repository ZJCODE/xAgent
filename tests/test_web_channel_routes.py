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
            static_dir=self.root / "missing-static",
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
        self.assertEqual(rows["voice"]["setup_hint"], "xagent voice setup")
        self.assertEqual(rows["feishu"]["setup_hint"], "xagent feishu setup")
        self.assertEqual(rows["weixin"]["setup_hint"], "xagent weixin setup")

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


if __name__ == "__main__":
    unittest.main()
