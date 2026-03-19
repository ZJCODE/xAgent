"""Tests for MessageHandler system prompt memory injection."""

import unittest

from xagent.core.handlers.message import MessageHandler
from xagent.schemas import Message, MessageType, RoleType, ToolCall


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

    def test_build_recent_transcript_message_collapses_messages(self):
        handler = MessageHandler(
            system_prompt="You are a helpful assistant.",
            message_storage=_FakeMessageStorage(),
        )
        messages = [
            Message.create("First question", role=RoleType.USER, sender_id="alice"),
            Message.create("First answer", role=RoleType.ASSISTANT, sender_id="agent:test"),
            Message(
                type=MessageType.FUNCTION_CALL_OUTPUT,
                role=RoleType.TOOL,
                sender_id="search_memory",
                content="Tool output preview",
                tool_call=ToolCall(call_id="call-1", output="full tool output"),
            ),
            Message.create("Latest question", role=RoleType.USER, sender_id="bob"),
        ]

        transcript_message = handler.build_recent_transcript_message(messages, current_user_id="bob")

        self.assertEqual(transcript_message["role"], "user")
        self.assertIsInstance(transcript_message["content"], str)
        self.assertIn("[speaker=alice role=user]", transcript_message["content"])
        self.assertIn("First answer", transcript_message["content"])
        self.assertIn("[speaker=bob role=user]", transcript_message["content"])
        self.assertNotIn("Tool output preview", transcript_message["content"])
        self.assertIn("latest message from bob", transcript_message["content"])

    def test_build_recent_transcript_message_keeps_latest_user_images(self):
        handler = MessageHandler(
            system_prompt="You are a helpful assistant.",
            message_storage=_FakeMessageStorage(),
        )
        messages = [
            Message.create("Need help with this screenshot", role=RoleType.USER, sender_id="alice"),
            Message.create(
                "Please inspect this image",
                role=RoleType.USER,
                sender_id="bob",
                image_source="https://example.com/screenshot.png",
            ),
        ]

        transcript_message = handler.build_recent_transcript_message(messages, current_user_id="bob")

        self.assertEqual(transcript_message["role"], "user")
        self.assertIsInstance(transcript_message["content"], list)
        self.assertEqual(transcript_message["content"][0]["type"], "input_text")
        self.assertEqual(transcript_message["content"][1]["type"], "input_image")
        self.assertEqual(
            transcript_message["content"][1]["image_url"],
            "https://example.com/screenshot.png",
        )


if __name__ == "__main__":
    unittest.main()
