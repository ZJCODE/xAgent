"""Local agent discovery for the inhabitant page."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from agents_world.http import NEIGHBORS_ROOT_ENV, page_neighbors_root
from agents_world.neighbors import list_local_agents


class NeighborDiscoveryTests(unittest.TestCase):
    def test_empty_root(self):
        with tempfile.TemporaryDirectory() as raw:
            self.assertEqual(list_local_agents(root=raw), [])

    def test_reads_registry_and_marks_stopped(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            agent_dir = root / "agents" / "agent1"
            agent_dir.mkdir(parents=True)
            (agent_dir / "config.yaml").write_text(
                "channels:\n  api:\n    host: 127.0.0.1\n    port: 59999\n",
                encoding="utf-8",
            )
            (root / "agents.yaml").write_text(
                "version: 1\nactive_agent: agent1\nagents:\n  agent1:\n    title: 一号\n    path: agents/agent1\n",
                encoding="utf-8",
            )
            neighbors = list_local_agents(root=root)
            self.assertEqual(len(neighbors), 1)
            self.assertEqual(neighbors[0]["name"], "agent1")
            self.assertEqual(neighbors[0]["title"], "一号")
            self.assertEqual(neighbors[0]["api_url"], "http://127.0.0.1:59999")
            self.assertFalse(neighbors[0]["running"])
            self.assertFalse(neighbors[0]["world_ready"])

    def test_page_neighbors_root_reads_env(self):
        old = os.environ.get(NEIGHBORS_ROOT_ENV)
        os.environ[NEIGHBORS_ROOT_ENV] = "/tmp/agents-world-neighbors-test"
        try:
            self.assertEqual(page_neighbors_root(), "/tmp/agents-world-neighbors-test")
        finally:
            if old is None:
                os.environ.pop(NEIGHBORS_ROOT_ENV, None)
            else:
                os.environ[NEIGHBORS_ROOT_ENV] = old


if __name__ == "__main__":
    unittest.main()
