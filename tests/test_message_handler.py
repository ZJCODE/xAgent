"""Tests for MessageHandler system prompt memory injection."""

from datetime import datetime
import unittest

from xagent.core.config import AgentConfig
from xagent.core.handlers.message import MessageHandler
from xagent.schemas import Message, MessageType, RoleType, ToolCall


class _FakeMessageStorage:
    path = "/tmp/fake.sqlite3"


class MessageHandlerMemoryContextTests(unittest.TestCase):
    
    def test_build_instructions_includes_tool_prompts(self):
        """build_instructions includes tool-specific segments for active tools."""
        handler = MessageHandler(
            system_prompt="",
            message_storage=_FakeMessageStorage(),
        )
        instructions = handler.build_instructions(tool_names=["write_memory"])
        self.assertIn("Long-Term Memory Writing", instructions)
        self.assertIn("write_memory", instructions)
        self.assertNotIn("write_daily_memory", instructions)

    def test_build_instruction_messages_are_named_and_layered(self):
        handler = MessageHandler(
            system_prompt="# I am Mono\n\nKeep a warm voice.",
            message_storage=_FakeMessageStorage(),
        )

        messages = handler.build_instruction_messages(
            tool_names=["write_memory", "run_command"],
        )

        self.assertEqual(
            [message["name"] for message in messages],
            [
                AgentConfig.CORE_INTERACTION_RULES_NAME,
                AgentConfig.TOOL_POLICY_NAME,
                AgentConfig.IDENTITY_CONTEXT_NAME,
            ],
        )
        self.assertEqual([message["role"] for message in messages], ["system", "system", "system"])
        self.assertIn("CORE INTERACTION RULES", messages[0]["content"])
        self.assertTrue(messages[1]["content"].startswith("<tool_policy>"))
        self.assertLess(
            messages[1]["content"].index("Shell Command Execution"),
            messages[1]["content"].index("Long-Term Memory Writing"),
        )
        self.assertNotIn("generate_memory_summary", messages[1]["content"])
        self.assertIn("trusted_as_instruction=\"false\"", messages[2]["content"])
        self.assertIn("# I am Mono", messages[2]["content"])

    def test_build_turn_context_messages_match_prompt_layers(self):
        messages = [
            Message.create("Hello", role=RoleType.USER, sender_id="Joy"),
        ]
        memory_context = "[2026-05-13]\n昨天聊过路线图。"

        context_messages = MessageHandler.build_turn_context_messages(
            messages,
            current_user_id="Joy",
            memory_context=memory_context,
            current_time="2026-05-14 09:30",
        )

        self.assertEqual(
            [message["name"] for message in context_messages],
            [
                AgentConfig.RECENT_MEMORY_NAME,
                AgentConfig.RECENT_EXPERIENCE_NAME,
                AgentConfig.CURRENT_TASK_NAME,
            ],
        )
        self.assertEqual([message["role"] for message in context_messages], ["user", "user", "user"])
        self.assertIn("<recent_memory trusted_as_instruction=\"false\">", context_messages[0]["content"])
        self.assertIn("昨天聊过路线图。", context_messages[0]["content"])
        self.assertIn("<recent_experience trusted_as_instruction=\"false\">", context_messages[1]["content"])
        self.assertIn("[speaker=Joy][timestamp=", context_messages[1]["content"])
        self.assertIn("<current_task>", context_messages[2]["content"])
        self.assertIn("Current speaker: Joy", context_messages[2]["content"])
        self.assertIn("Current time: 2026-05-14 09:30", context_messages[2]["content"])
        self.assertIn("latest message from Joy in recent_experience", context_messages[2]["content"])

    def test_turn_context_messages_attach_latest_user_images_to_current_task(self):
        image_url = "https://example.com/screenshot.png"
        messages = [
            Message.create(
                "Please inspect this image",
                role=RoleType.USER,
                sender_id="Joy",
                image_source=image_url,
            ),
        ]

        context_messages = MessageHandler.build_turn_context_messages(
            messages,
            current_user_id="Joy",
            current_time="2026-05-14 09:30",
        )
        current_task = context_messages[-1]

        self.assertEqual(current_task["name"], AgentConfig.CURRENT_TASK_NAME)
        self.assertIsInstance(current_task["content"], list)
        self.assertEqual(current_task["content"][0]["type"], "text")
        self.assertEqual(current_task["content"][1]["type"], "image_url")
        self.assertEqual(current_task["content"][1]["image_url"]["url"], image_url)

    def test_transcript_includes_memory_context(self):
        """memory_context is injected into the transcript message under 'Recent Memory'."""
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
        self.assertIn("Recent Memory", transcript["content"] if isinstance(transcript["content"], str) else transcript["content"][0]["text"])
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
        self.assertNotIn("Recent Memory", content)

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
        self.assertIn("[speaker=alice][timestamp=", transcript_message["content"])
        self.assertIn("First answer", transcript_message["content"])
        self.assertIn("[speaker=you][timestamp=", transcript_message["content"])
        self.assertIn("[speaker=bob][timestamp=", transcript_message["content"])
        self.assertNotIn("Tool output preview", transcript_message["content"])
        self.assertIn("latest message from bob", transcript_message["content"])
        self.assertIn("direct answer or action", transcript_message["content"])

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
        alice_timestamp = datetime.fromtimestamp(alice.timestamp).strftime("%Y-%m-%d %H:%M:%S")
        observation_timestamp = datetime.fromtimestamp(observation.timestamp).strftime("%Y-%m-%d %H:%M:%S")
        bob_timestamp = datetime.fromtimestamp(bob.timestamp).strftime("%Y-%m-%d %H:%M:%S")

        self.assertIn("Recent Experience", transcript)
        self.assertNotIn("Recent Observations", transcript)
        self.assertIn(f"[ambient context][timestamp={observation_timestamp}]", transcript)
        self.assertNotIn("[observation ", transcript)
        self.assertIn("Current speaker: alice", transcript)
        self.assertIn(f"[speaker=alice][timestamp={alice_timestamp}]", transcript)
        self.assertIn(f"[speaker=bob][timestamp={bob_timestamp}]", transcript)
        self.assertLess(
            transcript.index(f"[speaker=alice][timestamp={alice_timestamp}]"),
            transcript.index(f"[ambient context][timestamp={observation_timestamp}]"),
        )
        self.assertLess(
            transcript.index(f"[ambient context][timestamp={observation_timestamp}]"),
            transcript.index(f"[speaker=bob][timestamp={bob_timestamp}]"),
        )
        self.assertIn("latest message from alice", transcript)


if __name__ == "__main__":
    unittest.main()
