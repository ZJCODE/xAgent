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
    def _create_message_storage(self):
        return {"storage": "custom"}


class _FakeObservabilityRuntime:
    enabled = True

    def __init__(self):
        self.client_kwargs = None

    def create_client(self, client_kwargs):
        self.client_kwargs = dict(client_kwargs)
        return {"client": "wrapped"}


class BaseAgentRunnerStorageTests(unittest.TestCase):
    def test_runner_defaults_to_local_message_storage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            resolved_tmpdir = Path(tmpdir).resolve()
            config_path = Path(tmpdir) / "config.yaml"
            identity_path = Path(tmpdir) / "identity.md"
            config_path.write_text(
                "\n".join(
                    [
                        "provider:",
                        '  model: "gpt-5.4-mini"',
                    ]
                ),
                encoding="utf-8",
            )
            identity_path.write_text("You are a research assistant.", encoding="utf-8")

            with patch("xagent.interfaces.base.MessageStorageLocal", _FakeMessageStorageLocal):
                runner = _RunnerWithoutAgent(config_dir=str(resolved_tmpdir))

            self.assertEqual(
                runner.message_storage.path,
                str(resolved_tmpdir / "messages" / "messages.sqlite3"),
            )
            self.assertFalse(hasattr(runner, "memory_storage"))

    def test_runner_message_storage_factory_is_overridable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            identity_path = Path(tmpdir) / "identity.md"
            config_path.write_text(
                "\n".join(
                    [
                        "provider:",
                        '  model: "gpt-5.4-mini"',
                    ]
                ),
                encoding="utf-8",
            )
            identity_path.write_text("You are an extensible assistant.", encoding="utf-8")

            runner = _RunnerWithCustomStorage(config_dir=tmpdir)

            self.assertEqual(
                runner.message_storage,
                {"storage": "custom"},
            )

    def test_client_initialization_uses_observability_runtime(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            identity_path = Path(tmpdir) / "identity.md"
            config_path.write_text(
                "\n".join(
                    [
                        "provider:",
                        '  model: "gpt-5.4-mini"',
                    ]
                ),
                encoding="utf-8",
            )
            identity_path.write_text("You trace requests.", encoding="utf-8")
            runner = _RunnerWithoutAgent(config_dir=tmpdir)
            fake_observability = _FakeObservabilityRuntime()
            runner.observability = fake_observability

            client = runner._initialize_client({
                "provider": {
                    "model": "gpt-5.4-mini",
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "test-key",
                }
            })

            self.assertEqual(client, {"client": "wrapped"})
            self.assertEqual(
                fake_observability.client_kwargs,
                {
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "test-key",
                },
            )

    def test_disabled_observability_keeps_default_client_skip_behavior(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            identity_path = Path(tmpdir) / "identity.md"
            config_path.write_text(
                "\n".join(
                    [
                        "provider:",
                        '  model: "gpt-5.4-mini"',
                    ]
                ),
                encoding="utf-8",
            )
            identity_path.write_text("You use defaults.", encoding="utf-8")
            runner = _RunnerWithoutAgent(config_dir=tmpdir)

            client = runner._initialize_client({"provider": {"model": "gpt-5.4-mini"}})

            self.assertIsNone(client)


if __name__ == "__main__":
    unittest.main()
