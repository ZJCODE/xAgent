"""Tests for the web client server."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from xagent.interfaces.cli.agents import register_agent
from xagent.interfaces.web import WebClientServer


def _write_agent(path: Path, *, model: str, port: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    config = {
        "provider": {
            "name": "openai",
            "api_key": "test-key",
            "model": model,
        },
        "channels": {"api": {"host": "127.0.0.1", "port": port}},
    }
    (path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (path / "identity.md").write_text("# Identity\n\nTest agent.\n", encoding="utf-8")


class WebClientServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_web_client_serves_spa_shell(self):
        server = WebClientServer(
            host="127.0.0.1",
            port=1415,
            api_url="http://127.0.0.1:8010",
        )
        client = TestClient(server.app)

        for path in ("/", "/memory", "/workspace", "/message", "/agent", "/skills", "/tasks"):
            response = client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn("text/html", response.headers.get("content-type", ""))

    async def test_web_client_requires_ui_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            static_dir = Path(tmp) / "static"
            static_dir.mkdir()
            with self.assertRaises(FileNotFoundError):
                WebClientServer(
                    host="127.0.0.1",
                    port=1415,
                    api_url="http://127.0.0.1:8010",
                    static_dir=static_dir,
                )


class WebClientMultiAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.agent_a_path = self.root / "agents" / "agent_a"
        self.agent_b_path = self.root / "agents" / "agent_b"
        _write_agent(self.agent_a_path, model="agent-a-model", port=8010)
        _write_agent(self.agent_b_path, model="agent-b-model", port=9010)
        register_agent("agent_a", path=self.agent_a_path, make_active=True, root=self.root)
        register_agent("agent_b", path=self.agent_b_path, root=self.root)

    def _server(self) -> WebClientServer:
        return WebClientServer(
            host="127.0.0.1",
            port=1415,
            api_url="http://127.0.0.1:8010",
            config_dir=str(self.agent_a_path),
            initial_agent="agent_a",
            registry_root=self.root,
        )

    async def test_web_client_health_reports_api_reachability(self):
        client = TestClient(self._server().app)

        response = client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["web"])
        self.assertIn("api_reachable", payload)

    async def test_list_agents_endpoint_reports_both_agents(self):
        client = TestClient(self._server().app)

        response = client.get("/api/agents")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        names = {row["name"] for row in payload["agents"]}
        self.assertEqual(names, {"agent_a", "agent_b"})
        self.assertEqual(payload["selected_agent"], "agent_a")
        self.assertEqual(payload["active_agent"], "agent_a")

    async def test_admin_routes_follow_the_selected_agent(self):
        client = TestClient(self._server().app)

        initial = client.get("/api/agent/info")
        self.assertEqual(initial.status_code, 200)
        self.assertEqual(initial.json()["model"], "agent-a-model")

        select_response = client.post("/api/agents/select", json={"name": "agent_b"})
        self.assertEqual(select_response.status_code, 200)
        self.assertEqual(select_response.json()["selected_agent"], "agent_b")

        switched = client.get("/api/agent/info")
        self.assertEqual(switched.status_code, 200)
        self.assertEqual(switched.json()["model"], "agent-b-model")

    async def test_select_unknown_agent_returns_400(self):
        client = TestClient(self._server().app)

        response = client.post("/api/agents/select", json={"name": "nope"})

        self.assertEqual(response.status_code, 400)

    async def test_clear_messages_is_served_locally_without_a_channel(self):
        client = TestClient(self._server().app)

        response = client.post("/clear_messages")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "success")

    async def test_setup_schema_endpoint_returns_providers_and_models(self):
        client = TestClient(self._server().app)

        response = client.get("/api/agents/setup-schema")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("openai", {row["id"] for row in payload["providers"]})
        self.assertIn("gpt-5.4-mini", payload["models"]["openai"])
        self.assertEqual(
            ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
            [model for model in payload["models"]["openai"] if model.startswith("gpt-5.6-")],
        )
        self.assertTrue(all("Decide later" not in models for models in payload["models"].values()))
        self.assertIn("identity", payload["defaults"])

    async def test_create_agent_endpoint_registers_and_selects_new_agent(self):
        client = TestClient(self._server().app)
        payload = {
            "name": "agent_c",
            "title": "Agent C",
            "replace_existing": False,
            "selection": {
                "provider": "openai",
                "base_url": "https://api.openai.com/v1",
                "api_key": "test-key",
                "model": "gpt-5.4-mini",
                "identity": "# Identity\n\nCreated from web.\n",
            },
        }

        response = client.post("/api/agents", json=payload)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        names = {row["name"] for row in body["agents"]}
        self.assertIn("agent_c", names)
        self.assertEqual(body["selected_agent"], "agent_c")
        self.assertEqual(body["active_agent"], "agent_c")

        info = client.get("/api/agent/info")
        self.assertEqual(info.status_code, 200)
        self.assertEqual(info.json()["model"], "gpt-5.4-mini")

    async def test_create_duplicate_agent_returns_400(self):
        client = TestClient(self._server().app)
        payload = {
            "name": "agent_a",
            "selection": {
                "provider": "openai",
                "model": "gpt-5.4-mini",
                "identity": "# Identity\n\nDuplicate.\n",
            },
        }

        response = client.post("/api/agents", json=payload)

        self.assertEqual(response.status_code, 400)

    async def test_delete_agent_requires_matching_confirmation(self):
        client = TestClient(self._server().app)

        response = client.request(
            "DELETE",
            "/api/agents/agent_b",
            json={"confirm": "wrong"},
        )

        self.assertEqual(response.status_code, 400)

    async def test_delete_agent_removes_registry_entry_and_switches_selection(self):
        client = TestClient(self._server().app)
        client.post("/api/agents/select", json={"name": "agent_b"})

        response = client.request(
            "DELETE",
            "/api/agents/agent_b",
            json={"confirm": "agent_b"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        names = {row["name"] for row in body["agents"]}
        self.assertEqual(names, {"agent_a"})
        self.assertEqual(body["selected_agent"], "agent_a")


class WebClientEmptyRegistryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    async def test_web_client_serves_ui_with_empty_registry(self):
        server = WebClientServer(
            host="127.0.0.1",
            port=1415,
            api_url="http://127.0.0.1:8010",
            config_dir=str(self.root),
            registry_root=self.root,
        )
        client = TestClient(server.app)

        agents_response = client.get("/api/agents")
        self.assertEqual(agents_response.status_code, 200)
        payload = agents_response.json()
        self.assertEqual(payload["agents"], [])
        self.assertEqual(payload["selected_agent"], "")

        schema_response = client.get("/api/agents/setup-schema")
        self.assertEqual(schema_response.status_code, 200)

        shell_response = client.get("/")
        self.assertEqual(shell_response.status_code, 200)

        admin_response = client.get("/api/agent/info")
        self.assertEqual(admin_response.status_code, 404)
