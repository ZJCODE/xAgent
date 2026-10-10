"""Tools metadata must describe configuration offline and the owner online."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xagent.core.runtime.client import RuntimeUnavailable
from xagent.core.runtime.ownership import RuntimeOwnership
from xagent.interfaces.base import BaseAgentRunner
from xagent.interfaces.cli.agents import register_agent
from xagent.interfaces.server.admin_routes import register_admin_routes
from xagent.interfaces.server.admin_service import AdminService
from xagent.interfaces.web import WebClientServer


class AgentInfoToolsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.agent_dir = self.root / "agents" / "demo"
        self.agent_dir.mkdir(parents=True)
        self.config = {
            "provider": {"name": "openai", "model": "test-model", "api_key": "test-key"},
            "channels": {"api": {"enabled": False}},
        }
        self.write_config()
        (self.agent_dir / "identity.md").write_text("Test identity.\n", encoding="utf-8")
        register_agent("demo", path=self.agent_dir, make_active=True, root=self.root)

    def write_config(self):
        (self.agent_dir / "config.yaml").write_text(yaml.safe_dump(self.config), encoding="utf-8")

    def web(self):
        return WebClientServer(
            host="127.0.0.1", port=1415, api_url="http://127.0.0.1:8010",
            config_dir=str(self.agent_dir), initial_agent="demo", registry_root=self.root,
        )

    async def test_offline_tools_are_nonempty_without_agent_or_client_bootstrap(self):
        with patch("xagent.interfaces.base.BaseAgentRunner.__init__", side_effect=AssertionError("Agent bootstrap")), patch(
            "xagent.interfaces.base.BaseAgentRunner._initialize_client", side_effect=AssertionError("Client bootstrap")
        ), patch("xagent.interfaces.cli.agent_runtime.ensure_runtime") as ensure:
            with TestClient(self.web().app) as client:
                response = client.get("/api/agent/info")
        self.assertEqual(response.status_code, 200)
        info = response.json()
        self.assertEqual(info["tools_source"], "configured")
        self.assertTrue({"run_command", "manage_scheduled_tasks", "read_skill", "search_memory", "write_note", "see_image"}.issubset(info["tools"]))
        self.assertNotIn("web_search", info["tools"])
        self.assertNotIn("generate_image", info["tools"])
        ensure.assert_not_called()

    async def test_offline_metadata_matches_actual_tools_for_optional_features(self):
        cases = [
            ("none", "none", True, True),
            ("off", "disabled", False, False),
            ("openai", "openai", True, False),
            ("qwen_search", "qwen_images", False, True),
        ]
        for search, images, notes, vision in cases:
            with self.subTest(search=search, images=images, notes=notes, vision=vision):
                self.config["search"] = {"provider": search, "api_key": "feature-key"}
                self.config["image_generation"] = {"provider": images, "api_key": "feature-key"}
                self.config["agent"] = {"notes_enabled": notes}
                self.config["provider"]["supports_vision"] = vision
                self.write_config()
                with patch.object(BaseAgentRunner, "_initialize_client", return_value=object()), patch.object(
                    BaseAgentRunner, "_initialize_search_client", return_value=object()
                ), patch.object(BaseAgentRunner, "_initialize_image_generation_client", return_value=object()):
                    runner = BaseAgentRunner(config_dir=str(self.agent_dir))
                with TestClient(self.web().app) as client:
                    response = client.get("/api/agent/info")
                self.assertEqual(response.status_code, 200, response.text)
                info = response.json()
                self.assertEqual(info["tools"], list(runner.agent.tools))
                self.assertEqual(info["capabilities"]["vision"], vision)
                self.assertEqual(info["capabilities"]["web_search"], "web_search" in runner.agent.tools)
                self.assertEqual(info["capabilities"]["generate_image"], "generate_image" in runner.agent.tools)

    async def test_live_tools_use_owner_even_with_public_api_disabled_and_pending_config(self):
        with patch.object(BaseAgentRunner, "_initialize_client", return_value=object()):
            runner = BaseAgentRunner(config_dir=str(self.agent_dir))
        admin = AdminService(config_dir=str(self.agent_dir), agent=runner.agent)
        admin.is_runtime_owner = True
        owner_app = FastAPI()
        register_admin_routes(owner_app, lambda: admin)
        # Saved changes apply at restart. Loaded tools must remain authoritative.
        self.config["agent"] = {"notes_enabled": False}
        self.config["provider"]["supports_vision"] = False
        self.write_config()
        ownership = RuntimeOwnership(self.agent_dir).acquire()
        self.addCleanup(ownership.release)

        async def owner_request(method, path, **kwargs):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://owner") as client:
                return await client.request(method, path, **kwargs)

        socket_client = AsyncMock()
        socket_client.request.side_effect = owner_request
        with patch("xagent.core.runtime.client.RuntimeClient", return_value=socket_client) as constructor, patch(
            "xagent.interfaces.cli.agent_runtime.ensure_runtime"
        ) as ensure:
            with TestClient(self.web().app) as client:
                response = client.get("/api/agent/info")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["tools"], list(runner.agent.tools))
        self.assertEqual(response.json()["tools_source"], "runtime")
        self.assertIn("write_note", response.json()["tools"])
        self.assertTrue(response.json()["capabilities"]["vision"])
        constructor.assert_called_once_with(self.agent_dir)
        socket_client.request.assert_awaited_once_with("GET", "/api/agent/info", timeout=1.5)
        ensure.assert_not_called()

    async def test_unavailable_owner_falls_back_to_configuration_without_bootstrap(self):
        # Initialize only the data view before taking the ownership lock.
        web = self.web()
        web.session.get_current_admin()
        ownership = RuntimeOwnership(self.agent_dir).acquire()
        self.addCleanup(ownership.release)
        with patch("xagent.core.runtime.client.RuntimeClient.request", side_effect=RuntimeUnavailable("Starting")), patch(
            "xagent.interfaces.cli.agent_runtime.ensure_runtime"
        ) as ensure:
            with TestClient(web.app) as client:
                response = client.get("/api/agent/info")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["tools_source"], "configured")
        self.assertIn("search_memory", response.json()["tools"])
        ensure.assert_not_called()
