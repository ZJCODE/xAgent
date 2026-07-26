from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from xagent.core.agent_factory import AgentFactory, AgentPaths
from xagent.core.prompts import PromptAssembler
from xagent.settings import XAgentSettings


def _write_agent(root: Path) -> None:
    XAgentSettings.model_validate(
        {
            "schema_version": 2,
            "provider": {
                "name": "openai",
                "model": "test-model",
                "api_key": "test-key",
            },
            "channels": {
                "api": {"enabled": False, "host": "127.0.0.1", "port": 8010},
                "feishu": {"enabled": False},
                "weixin": {"enabled": False},
                "voice": {"enabled": False},
            },
        }
    ).write_atomic(root / AgentPaths.CONFIG_FILENAME)
    (root / AgentPaths.IDENTITY_FILENAME).write_text(
        "You are a concise independent individual.",
        encoding="utf-8",
    )


class AgentFactoryTests(unittest.TestCase):
    def test_factory_is_the_only_composition_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_agent(root)

            factory = AgentFactory(root)

            self.assertEqual(
                factory.message_storage.path.resolve(),
                (root / "state.sqlite3").resolve(),
            )
            self.assertEqual(factory.agent.identity, "You are a concise independent individual.")
            self.assertIn("run_command", factory.agent.tools)
            self.assertNotIn("manage_scheduled_tasks", factory.agent.tools)
            self.assertFalse((root / "messages").exists())
            self.assertFalse((root / "tasks").exists())

    def test_prompt_hard_limits_are_enforced(self):
        self.assertLessEqual(
            len(PromptAssembler.core_contract()),
            PromptAssembler.MAX_CORE_CHARS,
        )
        self.assertLessEqual(
            len(
                PromptAssembler.current_task(
                    speaker_id="person-1",
                    current_time="2026-07-25 12:00",
                )
            ),
            PromptAssembler.MAX_CURRENT_TASK_CHARS,
        )
        oversized_task = PromptAssembler.current_task(
            speaker_id="person",
            current_time="2026-07-25 12:00",
            channel_instructions="x" * 10_000,
        )
        self.assertLessEqual(
            len(oversized_task),
            PromptAssembler.MAX_CURRENT_TASK_CHARS,
        )
        self.assertIn("truncated", oversized_task)
        self.assertLessEqual(
            len(
                PromptAssembler.diary_task(
                    "old\n" + ("x" * 100_000) + "\nnew",
                    journal_date="2026-07-25",
                )
            ),
            PromptAssembler.MAX_INPUT_CHARS,
        )
