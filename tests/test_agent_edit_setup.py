"""Tests for web Edit Setup schema/apply aligned with CLI config_editor."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from xagent.interfaces.cli.config_editor import (
    apply_agent_edit_setup,
    build_agent_edit_setup_schema,
    load_config,
)
from xagent.interfaces.web.server import WebClientServer


def _write_agent(root: Path, *, anthropic: bool = False) -> Path:
    agent_dir = root / "agents" / "demo"
    agent_dir.mkdir(parents=True)
    provider = (
        {
            "name": "anthropic",
            "base_url": "https://api.anthropic.com",
            "api_key": "anthropic-key",
            "model": "claude-sonnet-4-20250514",
        }
        if anthropic
        else {
            "name": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key": "openai-key",
            "model": "gpt-5.4-mini",
        }
    )
    (agent_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "provider": provider,
                "search": {"provider": "none"},
                "channels": {"api": {"host": "127.0.0.1", "port": 8010}},
                "web": {"api_url": "http://127.0.0.1:8010"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (agent_dir / "identity.md").write_text("# Identity\n\nDemo.\n", encoding="utf-8")
    (root / "agents.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "active_agent": "demo",
                "agents": {"demo": {"path": str(agent_dir), "title": "Demo"}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return agent_dir


class AgentEditSetupHelperTests(unittest.TestCase):
    def test_schema_features_exclude_channel_setup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent_dir = _write_agent(Path(tmpdir))
            schema = build_agent_edit_setup_schema(load_config(agent_dir))

        self.assertEqual(
            [row["id"] for row in schema["features"]],
            ["model", "search", "observability"],
        )
        self.assertFalse(schema["features"][2]["disabled"])

    def test_observability_disabled_for_anthropic_model_api(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent_dir = _write_agent(Path(tmpdir), anthropic=True)
            schema = build_agent_edit_setup_schema(load_config(agent_dir))

        observability = next(row for row in schema["features"] if row["id"] == "observability")
        self.assertTrue(observability["disabled"])
        self.assertFalse(schema["observability"]["available"])

    def test_apply_search_and_observability(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent_dir = _write_agent(Path(tmpdir))
            search = apply_agent_edit_setup(agent_dir, "search", {"provider": "openai"})
            with self.assertRaisesRegex(ValueError, "Unsupported setup feature"):
                apply_agent_edit_setup(agent_dir, "image", {"provider": "openai"})
            observability = apply_agent_edit_setup(
                agent_dir,
                "observability",
                {
                    "enabled": True,
                    "public_key": "pk-lf-test",
                    "secret_key": "sk-lf-test",
                    "base_url": "https://cloud.langfuse.com",
                },
            )
            config = load_config(agent_dir)

        self.assertTrue(search["restart_required"])
        self.assertTrue(observability["changed"])
        self.assertEqual(config["search"]["provider"], "openai")
        self.assertNotIn("image_generation", config)
        self.assertTrue(config["observability"]["enabled"])

    def test_apply_model_update(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent_dir = _write_agent(Path(tmpdir))
            result = apply_agent_edit_setup(
                agent_dir,
                "model",
                {"provider": "deepseek", "model": "deepseek-v4-pro", "api_key": "deepseek-key"},
            )
            config = load_config(agent_dir)

        self.assertTrue(result["changed"])
        self.assertEqual(config["provider"]["name"], "deepseek")
        self.assertEqual(config["provider"]["model"], "deepseek-v4-pro")


class AgentEditSetupRouteTests(unittest.TestCase):
    def test_web_routes_expose_schema_and_apply(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            agent_dir = _write_agent(root)
            server = WebClientServer(
                host="127.0.0.1",
                port=1416,
                api_url="http://127.0.0.1:8010",
                config_dir=str(agent_dir),
                registry_root=root,
            )
            client = TestClient(server.app)

            schema_response = client.get("/api/agent/setup-schema")
            apply_response = client.post("/api/agent/setup/search", json={"provider": "qwen", "api_key": "qwen-key"})

        self.assertEqual(schema_response.status_code, 200)
        self.assertEqual(apply_response.status_code, 200)
        self.assertEqual(apply_response.json()["feature"], "search")
        self.assertTrue(apply_response.json()["restart_required"])


if __name__ == "__main__":
    unittest.main()
