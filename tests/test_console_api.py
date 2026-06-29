import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import yaml
from fastapi.testclient import TestClient

from xagent.interfaces.server import ConsoleHTTPServer


def _create_payload(name: str = "work") -> dict:
    return {
        "name": name,
        "title": "Work Agent",
        "make_active": True,
        "identity": "# Identity\n\nYou are the work agent.\n",
        "model": {
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key": "test-key",
            "model": "gpt-5.4-mini",
            "supports_vision": False,
        },
        "capabilities": {
            "search_provider": "none",
            "image_generation_provider": "none",
            "observability_enabled": False,
        },
        "voice": {
            "enabled": False,
            "provider": "none",
        },
    }


class ConsoleAPITests(unittest.IsolatedAsyncioTestCase):
    async def _client(self, server: ConsoleHTTPServer):
        transport = httpx.ASGITransport(app=server.app)
        return httpx.AsyncClient(transport=transport, base_url="http://testserver")

    async def test_lists_empty_registry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("xagent.interfaces.cli.agents.BaseAgentConfig.DEFAULT_CONFIG_DIR", str(Path(tmpdir))):
                server = ConsoleHTTPServer(enable_web=False)

                async with await self._client(server) as client:
                    response = await client.get("/api/console/agents")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"active_agent": "", "agents": []})

    async def test_create_select_redact_and_delete_agent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            with patch("xagent.interfaces.cli.agents.BaseAgentConfig.DEFAULT_CONFIG_DIR", str(root)):
                with patch("sys.stdout", new_callable=io.StringIO):
                    server = ConsoleHTTPServer(enable_web=False)
                    async with await self._client(server) as client:
                        created = await client.post("/api/console/agents", json=_create_payload())
                        listed = await client.get("/api/console/agents")
                        selected = await client.post("/api/console/agents/work/select")
                        config_response = await client.get("/api/console/agents/work/config")
                        config_path = root / "agents" / "work" / "config.yaml"
                        written_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                        deleted = await client.delete("/api/console/agents/work")

                agent_path_exists_after_delete = (root / "agents" / "work").exists()

        self.assertEqual(created.status_code, 200)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(selected.status_code, 200)
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(created.json()["active_agent"], "work")
        self.assertEqual(written_config["channels"]["api"]["port"], 8011)
        self.assertEqual(config_response.json()["config"]["provider"]["api_key"], "********")
        self.assertFalse(agent_path_exists_after_delete)

    async def test_create_allocates_next_api_port(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            with patch("xagent.interfaces.cli.agents.BaseAgentConfig.DEFAULT_CONFIG_DIR", str(root)):
                with patch("sys.stdout", new_callable=io.StringIO):
                    server = ConsoleHTTPServer(enable_web=False)
                    async with await self._client(server) as client:
                        first = await client.post("/api/console/agents", json=_create_payload("alpha"))
                        second = await client.post("/api/console/agents", json=_create_payload("beta"))

                alpha_config = yaml.safe_load((root / "agents" / "alpha" / "config.yaml").read_text(encoding="utf-8"))
                beta_config = yaml.safe_load((root / "agents" / "beta" / "config.yaml").read_text(encoding="utf-8"))

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(alpha_config["channels"]["api"]["port"], 8011)
        self.assertEqual(beta_config["channels"]["api"]["port"], 8012)

    async def test_start_api_reassigns_reserved_console_port(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            with patch("xagent.interfaces.cli.agents.BaseAgentConfig.DEFAULT_CONFIG_DIR", str(root)):
                with patch("sys.stdout", new_callable=io.StringIO):
                    server = ConsoleHTTPServer(enable_web=False)
                    async with await self._client(server) as client:
                        created = await client.post("/api/console/agents", json=_create_payload())
                        config_path = root / "agents" / "work" / "config.yaml"
                        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                        config["channels"]["api"]["port"] = 8010
                        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
                        with patch("xagent.interfaces.server.console_services.cli_runtime.handle_start", return_value=0) as starter:
                            started = await client.post("/api/console/agents/work/channels/api/start")
                        updated_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        self.assertEqual(created.status_code, 200)
        self.assertEqual(started.status_code, 200)
        self.assertEqual(updated_config["channels"]["api"]["port"], 8011)
        starter.assert_called_once()

    def test_chat_websocket_requires_running_api_channel(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            with patch("xagent.interfaces.cli.agents.BaseAgentConfig.DEFAULT_CONFIG_DIR", str(root)):
                with patch("sys.stdout", new_callable=io.StringIO):
                    server = ConsoleHTTPServer(enable_web=False)
                    with TestClient(server.app) as client:
                        create_response = client.post("/api/console/agents", json=_create_payload())
                        with client.websocket_connect("/ws/console/agents/work/chat") as websocket:
                            first = websocket.receive_json()
                            second = websocket.receive_json()

        self.assertEqual(create_response.status_code, 200)
        self.assertEqual(first["code"], "channel_required")
        self.assertEqual(second, {"type": "done"})


if __name__ == "__main__":
    unittest.main()
