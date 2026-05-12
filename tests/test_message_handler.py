"""Tests for MessageHandler system prompt memory injection."""

import unittest

from xagent.core.handlers.message import MessageHandler
from xagent.schemas import Message, MessageType, RoleType, ToolCall


class _FakeMessageStorage:
    path = "/tmp/fake.sqlite3"


class MessageHandlerMemoryContextTests(unittest.TestCase):
    def test_build_instructions_contains_core_rules_and_dev_prompt(self):
        """build_instructions returns static layers: core rules + input format + developer prompt."""
        handler = MessageHandler(
            system_prompt="You are a helpful assistant.",
            message_storage=_FakeMessageStorage(),
        )
        instructions = handler.build_instructions()
        self.assertIn("Core Rules", instructions)
        self.assertIn("Conversation Awareness", instructions)
        self.assertIn("You are a helpful assistant.", instructions)
        # Should NOT contain per-turn dynamic content
        self.assertNotIn("Recent Diary Memory", instructions)

    def test_build_instructions_includes_tool_prompts(self):
        """build_instructions includes tool-specific segments for active tools."""
        handler = MessageHandler(
            system_prompt="",
            message_storage=_FakeMessageStorage(),
        )
        instructions = handler.build_instructions(tool_names=["write_daily_memory"])
        self.assertIn("Daily Memory Writing", instructions)

    def test_transcript_includes_memory_context(self):
        """memory_context is injected into the transcript message under 'Recent Diary Memory'."""
        handler = MessageHandler(
            system_prompt="You are a helpful assistant.",
            message_storage=_FakeMessageStorage(),
        )
        messages = [
            Message.create("Hello", role=RoleType.USER, sender_id="alice"),
        ]
        memory_context = "[2026-03-18]\n今天主要围绕路线图推进。"
        transcript = handler.build_recent_transcript_message(
            messages,
            current_user_id="alice",
            memory_context=memory_context,
        )
        self.assertIn("Recent Diary Memory", transcript["content"] if isinstance(transcript["content"], str) else transcript["content"][0]["text"])
        self.assertIn("[2026-03-18]", transcript["content"] if isinstance(transcript["content"], str) else transcript["content"][0]["text"])
        self.assertIn("今天主要围绕路线图推进。", transcript["content"] if isinstance(transcript["content"], str) else transcript["content"][0]["text"])

    def test_transcript_omits_memory_section_when_context_empty(self):
        """Empty memory_context should not inject a memory section in transcript."""
        handler = MessageHandler(
            system_prompt="You are a helpful assistant.",
            message_storage=_FakeMessageStorage(),
        )
        messages = [
            Message.create("Hello", role=RoleType.USER, sender_id="alice"),
        ]
        transcript = handler.build_recent_transcript_message(
            messages,
            current_user_id="alice",
            memory_context="",
        )
        content = transcript["content"] if isinstance(transcript["content"], str) else transcript["content"][0]["text"]
        self.assertNotIn("Recent Diary Memory", content)

    def test_build_recent_transcript_message_contains_runtime_context(self):
        handler = MessageHandler(
            system_prompt="You are a helpful assistant.",
            message_storage=_FakeMessageStorage(),
        )
        messages = [
            Message.create("Hello", role=RoleType.USER, sender_id="alice"),
        ]
        transcript = handler.build_recent_transcript_message(messages, current_user_id="alice")
        content = transcript["content"] if isinstance(transcript["content"], str) else transcript["content"][0]["text"]
        self.assertIn("Current speaker: alice", content)
        self.assertIn("Date:", content)

    def test_build_recent_transcript_message_collapses_messages(self):
        handler = MessageHandler(
            system_prompt="You are a helpful assistant.",
            message_storage=_FakeMessageStorage(),
        )
        messages = [
            Message.create("First question", role=RoleType.USER, sender_id="alice"),
            Message.create("First answer", role=RoleType.ASSISTANT, sender_id="agent"),
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
        self.assertIn("[speaker=alice]", transcript_message["content"])
        self.assertIn("First answer", transcript_message["content"])
        self.assertIn("[speaker=you]", transcript_message["content"])
        self.assertIn("[speaker=bob]", transcript_message["content"])
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
        self.assertEqual(transcript_message["content"][0]["type"], "text")
        self.assertEqual(transcript_message["content"][1]["type"], "image_url")
        self.assertEqual(
            transcript_message["content"][1]["image_url"]["url"],
            "https://example.com/screenshot.png",
        )

    def test_observations_are_interleaved_in_recent_experience(self):
        alice = Message.create("Hi", role=RoleType.USER, sender_id="alice")
        alice.timestamp = 1.0
        observation = Message.create_context_event(
            "Bob mentioned the room is getting noisy.",
            source="microphone",
            event_type="overheard_speech",
            metadata={
                "speaker_id": "bob",
                "addressed_to_agent": False,
            },
        )
        observation.timestamp = 2.0
        bob = Message.create("Can you hear that?", role=RoleType.USER, sender_id="bob")
        bob.timestamp = 3.0

        transcript = MessageHandler.build_recent_transcript_message(
            [bob, observation, alice],
            current_user_id="alice",
        )["content"]

        self.assertIn("Recent Experience", transcript)
        self.assertNotIn("Recent Observations", transcript)
        self.assertIn("[ambient context]", transcript)
        self.assertNotIn("[observation ", transcript)
        self.assertIn("Current speaker: alice", transcript)
        self.assertIn("[speaker=alice]", transcript)
        self.assertIn("[speaker=bob]", transcript)
        self.assertLess(transcript.index("[speaker=alice]"), transcript.index("[ambient context]"))
        self.assertLess(transcript.index("[ambient context]"), transcript.index("[speaker=bob]"))
        self.assertIn("latest message from alice", transcript)


if __name__ == "__main__":
    unittest.main()
