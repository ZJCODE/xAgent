"""Tests for BaseAgentRunner initialization (message storage only)."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xagent.interfaces.base import BaseAgentRunner


class _FakeMessageStorageLocal:
    def __init__(self, path: str):
        self.path = path


class _RunnerWithoutAgent(BaseAgentRunner):
    def _initialize_agent(self):
        return object()


class _RunnerWithCustomStorage(_RunnerWithoutAgent):
    def _create_message_storage(self, *, agent_name: str, agent_slug: str):
        return {"agent_name": agent_name, "agent_slug": agent_slug}


class BaseAgentRunnerStorageTests(unittest.TestCase):
    def test_runner_defaults_to_local_message_storage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            resolved_tmpdir = Path(tmpdir).resolve()
            config_path = Path(tmpdir) / "config.yaml"
            identity_path = Path(tmpdir) / "identity.md"
            config_path.write_text(
                "\n".join(
                    [
                        "agent:",
                        '  name: "Research Agent"',
                        "  provider:",
                        '    model: "gpt-5.4-mini"',
                    ]
                ),
                encoding="utf-8",
            )
            identity_path.write_text("You are a research assistant.", encoding="utf-8")

            with patch("xagent.interfaces.base.MessageStorageLocal", _FakeMessageStorageLocal):
                runner = _RunnerWithoutAgent(config_dir=str(resolved_tmpdir))

            self.assertEqual(runner.message_storage.path, str(resolved_tmpdir / "research_agent_messages.sqlite3"))
            self.assertFalse(hasattr(runner, "memory_storage"))

    def test_runner_message_storage_factory_is_overridable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            identity_path = Path(tmpdir) / "identity.md"
            config_path.write_text(
                "\n".join(
                    [
                        "agent:",
                        '  name: "Extensible Agent"',
                        "  provider:",
                        '    model: "gpt-5.4-mini"',
                    ]
                ),
                encoding="utf-8",
            )
            identity_path.write_text("You are an extensible assistant.", encoding="utf-8")

            runner = _RunnerWithCustomStorage(config_dir=tmpdir)

            self.assertEqual(
                runner.message_storage,
                {"agent_name": "Extensible Agent", "agent_slug": "extensible_agent"},
            )


if __name__ == "__main__":
    unittest.main()
