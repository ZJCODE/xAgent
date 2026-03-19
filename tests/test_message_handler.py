"""Tests for MessageHandler system prompt memory injection."""

import unittest

from xagent.core.handlers.message import MessageHandler


class _FakeMessageStorage:
    path = "/tmp/fake.sqlite3"


class MessageHandlerMemoryContextTests(unittest.TestCase):
    def test_build_system_prompt_includes_memory_context(self):
        """memory_context is injected under the 'Recent Diary Memory' header."""
        handler = MessageHandler(
            system_prompt="You are a helpful assistant.",
            message_storage=_FakeMessageStorage(),
        )
        memory_context = "[2026-03-18]\n今天主要围绕路线图推进。"
        prompt = handler.build_system_prompt(
            user_id="alice",
            memory_context=memory_context,
        )
        self.assertIn("Recent Diary Memory", prompt)
        self.assertIn("[2026-03-18]", prompt)
        self.assertIn("今天主要围绕路线图推进。", prompt)

    def test_build_system_prompt_omits_section_when_context_empty(self):
        """Empty memory_context should not inject a memory section."""
        handler = MessageHandler(
            system_prompt="You are a helpful assistant.",
            message_storage=_FakeMessageStorage(),
        )
        prompt = handler.build_system_prompt(
            user_id="alice",
            memory_context="",
        )
        self.assertNotIn("Recent Diary Memory", prompt)


if __name__ == "__main__":
    unittest.main()
