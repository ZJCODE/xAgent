import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xagent.interfaces.base import BaseAgentRunner


class _FakeMessageStorageLocal:
    def __init__(self, path: str):
        self.path = path


class _FakeMemoryStorageLocal:
    def __init__(self, path: str, collection_name: str | None = None):
        self.path = path
        self.collection_name = collection_name


class _RunnerWithoutAgent(BaseAgentRunner):
    def _initialize_agent(self):
        return object()


class _RunnerWithCustomStorage(_RunnerWithoutAgent):
    def _create_message_storage(self, *, agent_name: str, agent_slug: str):
        return {"agent_name": agent_name, "agent_slug": agent_slug}

    def _create_memory_storage(self, *, agent_name: str):
        return {"agent_name": agent_name, "backend": "custom"}


class BaseAgentRunnerStorageTests(unittest.TestCase):
    def test_runner_defaults_to_local_storage_even_with_legacy_storage_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            resolved_tmpdir = Path(tmpdir).resolve()
            config_path = Path(tmpdir) / "agent.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "agent:",
                        '  name: "Research Agent"',
                        '  storage_mode: "cloud"',
                        f'  workspace: "{resolved_tmpdir}"',
                        "server: {}",
                    ]
                ),
                encoding="utf-8",
            )

            with patch("xagent.interfaces.base.MessageStorageLocal", _FakeMessageStorageLocal), patch(
                "xagent.interfaces.base.MemoryStorageLocal", _FakeMemoryStorageLocal
            ):
                runner = _RunnerWithoutAgent(config_path=str(config_path))

            self.assertEqual(runner.message_storage.path, str(resolved_tmpdir / "research_agent_messages.sqlite3"))
            self.assertEqual(
                runner.memory_storage.path,
                str(resolved_tmpdir / "research_agent_messages.sqlite3"),
            )
            self.assertIsNone(runner.memory_storage.collection_name)

    def test_runner_storage_factories_are_overridable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "agent.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "agent:",
                        '  name: "Extensible Agent"',
                        f'  workspace: "{tmpdir}"',
                        "server: {}",
                    ]
                ),
                encoding="utf-8",
            )

            runner = _RunnerWithCustomStorage(config_path=str(config_path))

            self.assertEqual(
                runner.message_storage,
                {"agent_name": "Extensible Agent", "agent_slug": "extensible_agent"},
            )
            self.assertEqual(
                runner.memory_storage,
                {"agent_name": "Extensible Agent", "backend": "custom"},
            )
