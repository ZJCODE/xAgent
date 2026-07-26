from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import yaml

from xagent.interfaces.cli.agents import register_agent
from xagent.interfaces.web.server import WebClientServer


def _write_agent(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "provider": {
                    "name": "openai",
                    "api_key": "test-key",
                    "model": "gpt-5.4-mini",
                },
                "channels": {
                    "api": {"enabled": False, "host": "127.0.0.1", "port": 8010},
                    "feishu": {"enabled": False, "app_id": "", "app_secret": ""},
                    "weixin": {"enabled": False, "account_id": ""},
                    "voice": {"enabled": False},
                },
            }
        ),
        encoding="utf-8",
    )
    (path / "identity.md").write_text("# Identity\n\nTest agent.\n", encoding="utf-8")


class WebChannelRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.agent = self.root / "agents" / "agent"
        _write_agent(self.agent)
        register_agent("agent", path=self.agent, make_active=True, root=self.root)
        self.server = WebClientServer(
            host="127.0.0.1",
            port=1415,
            config_dir=str(self.agent),
            initial_agent="agent",
            registry_root=self.root,
        )

    async def client(self):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.server.app),
            base_url="http://web",
        )

    async def test_list_channels_distinguishes_runtime_from_configuration(self):
        async with await self.client() as client:
            response = await client.get("/api/channels")
        rows = {row["id"]: row for row in response.json()["channels"]}
        self.assertEqual(rows["api"]["status"], "runtime-stopped")
        self.assertTrue(rows["api"]["ready"])
        self.assertFalse(rows["feishu"]["ready"])

    async def test_start_forwards_to_the_single_runtime(self):
        snapshot = {
            "pid": 42,
            "channels": [
                {"name": "api", "state": "running", "enabled": True, "error": ""}
            ],
        }
        with patch(
            "xagent.interfaces.web.channel_routes._runtime_snapshot",
            new=AsyncMock(side_effect=[None, snapshot]),
        ), patch(
            "xagent.interfaces.web.channel_routes.RuntimeLauncher.start",
            return_value=SimpleNamespace(state="started", pid=42),
        ), patch(
            "xagent.interfaces.web.channel_routes.RuntimeClient.request",
            return_value={"channel": snapshot["channels"][0]},
        ) as request:
            async with await self.client() as client:
                response = await client.post("/api/channels/api/start")
        self.assertEqual(response.status_code, 200)
        request.assert_called_once_with("POST", "/v1/channels/api/start")

    async def test_channel_setup_uses_the_shared_settings_writer(self):
        async with await self.client() as client:
            response = await client.post(
                "/api/channels/feishu/setup",
                json={
                    "selection": {
                        "credential_mode": "manual",
                        "app_id": "app",
                        "app_secret": "secret",
                        "stream": False,
                        "group_fetch_limit": 10,
                        "group_reply_only_when_mentioned": True,
                    }
                },
            )
        self.assertEqual(response.status_code, 200)
        config = yaml.safe_load((self.agent / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(config["channels"]["feishu"]["app_id"], "app")
