"""Tests for the web client's multi-agent session state."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from xagent.interfaces.cli.agents import AgentRegistryError, register_agent
from xagent.interfaces.clients.web.session import WebAgentSession


def _write_agent(path: Path, *, port: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    config = {
        "provider": {
            "name": "openai",
            "api_key": "test-key",
            "model": "gpt-5.4-mini",
        },
        "channels": {"api": {"host": "127.0.0.1", "port": port}},
    }
    (path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (path / "identity.md").write_text("# Identity\n\nTest agent.\n", encoding="utf-8")


class WebAgentSessionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.agent_a_path = self.root / "agents" / "agent_a"
        self.agent_b_path = self.root / "agents" / "agent_b"
        _write_agent(self.agent_a_path, port=8010)
        _write_agent(self.agent_b_path, port=9010)
        register_agent("agent_a", path=self.agent_a_path, make_active=True, root=self.root)
        register_agent("agent_b", path=self.agent_b_path, root=self.root)

    def _session(self) -> WebAgentSession:
        return WebAgentSession(
            initial_config_dir=self.agent_a_path,
            initial_agent_name="agent_a",
            initial_api_url="http://127.0.0.1:8010",
            registry_root=self.root,
        )

    def test_list_agents_reports_active_selected_and_initialized(self):
        session = self._session()
        rows = {row["name"]: row for row in session.list_agents()}

        self.assertEqual(set(rows), {"agent_a", "agent_b"})
        self.assertTrue(rows["agent_a"]["active"])
        self.assertTrue(rows["agent_a"]["selected"])
        self.assertTrue(rows["agent_a"]["initialized"])
        self.assertFalse(rows["agent_b"]["active"])
        self.assertFalse(rows["agent_b"]["selected"])
        self.assertFalse(rows["agent_b"]["channel_running"])

    def test_select_switches_current_agent(self):
        session = self._session()
        self.assertEqual(session.current_agent_name, "agent_a")

        snapshot = session.select("agent_b")

        self.assertEqual(session.current_agent_name, "agent_b")
        self.assertEqual(snapshot["selected_agent"], "agent_b")
        self.assertEqual(snapshot["active_agent"], "agent_a")

    def test_select_unknown_agent_raises(self):
        session = self._session()
        with self.assertRaises(AgentRegistryError):
            session.select("does-not-exist")
        self.assertEqual(session.current_agent_name, "agent_a")

    def test_get_current_admin_is_cached_per_agent_and_distinct_across_agents(self):
        session = self._session()

        admin_a_first = session.get_current_admin()
        admin_a_second = session.get_current_admin()
        self.assertIs(admin_a_first, admin_a_second)

        session.select("agent_b")
        admin_b = session.get_current_admin()

        self.assertIsNot(admin_a_first, admin_b)
        self.assertEqual(Path(admin_a_first.config_dir).resolve(), self.agent_a_path.resolve())
        self.assertEqual(Path(admin_b.config_dir).resolve(), self.agent_b_path.resolve())

        session.select("agent_a")
        self.assertIs(session.get_current_admin(), admin_a_first)

    def test_get_current_config_dir_tracks_selected_agent(self):
        session = self._session()

        self.assertEqual(session.get_current_config_dir(), self.agent_a_path.resolve())

        session.select("agent_b")

        self.assertEqual(session.get_current_config_dir(), self.agent_b_path.resolve())

    def test_get_current_api_url_uses_initial_override_then_recomputes_per_agent(self):
        session = self._session()

        self.assertEqual(session.get_current_api_url(), "http://127.0.0.1:8010")

        session.select("agent_b")
        self.assertEqual(session.get_current_api_url(), "http://127.0.0.1:9010")

        session.select("agent_a")
        self.assertEqual(session.get_current_api_url(), "http://127.0.0.1:8010")


if __name__ == "__main__":
    unittest.main()
