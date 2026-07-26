from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from xagent.interfaces.cli.agents import AgentRegistryError, register_agent
from xagent.interfaces.web.session import SECRET_SENTINEL, WebAgentSession
from xagent.settings import XAgentSettings


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
            }
        ),
        encoding="utf-8",
    )
    (path / "identity.md").write_text("# Identity\n\nTest agent.\n", encoding="utf-8")


class WebAgentSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.agent_a = self.root / "agents" / "agent_a"
        self.agent_b = self.root / "agents" / "agent_b"
        _write_agent(self.agent_a)
        _write_agent(self.agent_b)
        register_agent("agent_a", path=self.agent_a, make_active=True, root=self.root)
        register_agent("agent_b", path=self.agent_b, root=self.root)

    def session(self) -> WebAgentSession:
        return WebAgentSession(
            initial_config_dir=self.agent_a,
            initial_agent_name="agent_a",
            registry_root=self.root,
        )

    def test_selection_only_changes_the_web_control_target(self):
        session = self.session()
        self.assertEqual(session.get_current_config_dir(), self.agent_a.resolve())

        snapshot = session.select("agent_b")

        self.assertEqual(snapshot["selected_agent"], "agent_b")
        self.assertEqual(snapshot["active_agent"], "agent_a")
        self.assertEqual(session.get_current_config_dir(), self.agent_b.resolve())

    def test_unknown_selection_is_rejected(self):
        with self.assertRaises(AgentRegistryError):
            self.session().select("unknown")

    def test_snapshot_contains_no_per_channel_api_address(self):
        rows = self.session().snapshot()["agents"]
        self.assertEqual({row["name"] for row in rows}, {"agent_a", "agent_b"})
        self.assertTrue(all("api_url" not in row for row in rows))
        self.assertTrue(all(row["runtime_running"] is False for row in rows))
        self.assertTrue(all(row["provider"] == "openai" for row in rows))
        self.assertTrue(all(row["model"] == "gpt-5.4-mini" for row in rows))
        self.assertTrue(all(row["pid"] is None for row in rows))

    def test_settings_are_editable_without_exposing_or_erasing_secrets(self):
        session = self.session()
        snapshot = session.settings_snapshot()

        self.assertEqual(snapshot["settings"]["provider"]["api_key"], SECRET_SENTINEL)
        self.assertNotIn("test-key", str(snapshot))

        submitted = snapshot["settings"]
        submitted["agent"]["max_iter"] = 17
        updated = session.update_settings(submitted)
        saved = XAgentSettings.load(self.agent_a / "config.yaml")

        self.assertEqual(saved.provider.api_key, "test-key")
        self.assertEqual(saved.agent.max_iter, 17)
        self.assertEqual(updated["settings"]["provider"]["api_key"], SECRET_SENTINEL)
