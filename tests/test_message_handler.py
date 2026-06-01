"""Tests for MessageHandler system prompt memory injection."""

import base64
from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from xagent.core.config import AgentConfig
from xagent.core.handlers.message import MessageHandler
from xagent.schemas import Message, MessageType, RoleType, ToolCall
from xagent.utils.image_utils import data_uri_to_bytes, extract_image_urls_from_text


class _FakeMessageStorage:
    path = "/tmp/fake.sqlite3"

    def __init__(self):
        self.messages = []

    async def add_messages(self, *messages):
        self.messages.extend(messages)


class MessageHandlerMemoryContextTests(unittest.TestCase):
    def test_handler_persists_data_uri_as_metadata_and_blob_source(self):
        image_bytes = b"\x89PNG\r\n\x1a\npng"
        image_source = f"data:image/png;base64,{base64.b64encode(image_bytes).decode('ascii')}"

        with tempfile.TemporaryDirectory() as tmpdir:
            handler = MessageHandler(
                message_storage=_FakeMessageStorage(),
                workspace_dir=tmpdir,
            )
            normalized_sources, image_metadata = handler._prepare_message_images([image_source])
            msg = Message.create(
                "inspect this",
                role=RoleType.USER,
                sender_id="Joy",
                image_source=normalized_sources,
            )
            msg.metadata["images"] = image_metadata

            image = msg.multimodal.image
            self.assertFalse(isinstance(image, list))
            self.assertTrue(image.source.startswith("/api/workspace/blob?path=temp%2Fimages%2Finbound%2F"))
            self.assertEqual(len(msg.metadata["images"]), 1)
            asset = msg.metadata["images"][0]
            self.assertTrue(asset["workspace_path"].startswith("temp/images/inbound/"))
            self.assertIn("/api/workspace/blob?path=temp%2Fimages%2Finbound%2F", asset["blob_url"])
            self.assertEqual((Path(tmpdir) / asset["workspace_path"]).read_bytes(), image_bytes)

            current_images = MessageHandler._current_message_images(msg, "Joy", workspace_dir=tmpdir)
            self.assertEqual(data_uri_to_bytes(current_images[0])[0], image_bytes)

    def test_workspace_blob_markdown_is_detected_as_image_input(self):
        blob_url = "/api/workspace/blob?path=temp%2Fimages%2Fresult.png"

        detected = extract_image_urls_from_text(f"please inspect ![Generated image]({blob_url})")

        self.assertEqual(detected, [blob_url])

    def test_store_user_message_persists_attachment_manifest_metadata(self):
        import asyncio

        storage = _FakeMessageStorage()
        handler = MessageHandler(
            message_storage=storage,
            workspace_dir="/tmp/workspace",
        )

        msg = asyncio.run(handler.store_user_message(
            "please inspect this",
            "Joy",
            attachments=[{
                "kind": "file",
                "path": "reports/out.pdf",
                "blob_url": "/api/workspace/blob?path=reports%2Fout.pdf",
                "mime_type": "application/pdf",
                "file_name": "out.pdf",
                "size_bytes": 4,
            }],
        ))

        self.assertIn("Attached files:", msg.content)
        self.assertIn("[out.pdf](/api/workspace/blob?path=reports%2Fout.pdf)", msg.content)
        self.assertIn("path: reports/out.pdf", msg.content)
        self.assertEqual(msg.metadata["attachments"][0]["path"], "reports/out.pdf")
        self.assertEqual(storage.messages, [msg])

    def test_store_user_message_promotes_workspace_image_source_to_attachment(self):
        import asyncio

        image_bytes = b"\x89PNG\r\n\x1a\nsmall"
        image_source = f"data:image/png;base64,{base64.b64encode(image_bytes).decode('ascii')}"

        with tempfile.TemporaryDirectory() as tmpdir:
            storage = _FakeMessageStorage()
            handler = MessageHandler(
                message_storage=storage,
                workspace_dir=tmpdir,
            )

            msg = asyncio.run(handler.store_user_message(
                "rotate this image",
                "Joy",
                image_source=image_source,
            ))

            attachment = msg.metadata["attachments"][0]
            self.assertEqual(attachment["kind"], "image")
            self.assertTrue(attachment["path"].startswith("temp/images/inbound/"))
            self.assertIn("Attached files:", msg.content)
            self.assertIn(attachment["blob_url"], msg.content)
            self.assertIn(f"path: {attachment['path']}", msg.content)
            self.assertEqual((Path(tmpdir) / attachment["path"]).read_bytes(), image_bytes)
            self.assertEqual(storage.messages, [msg])
    
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

    def test_tool_policy_directs_images_and_artifacts_to_attachments(self):
        handler = MessageHandler(
            system_prompt="",
            message_storage=_FakeMessageStorage(),
        )

        messages = handler.build_instruction_messages(
            tool_names=["generate_image", "attach_artifact"],
        )
        tool_policy = messages[1]["content"]

        self.assertIn("structured attachment metadata", tool_policy)
        self.assertIn("Do not embed them in reply text with Markdown image syntax", tool_policy)
        self.assertIn("attach the workspace file instead", tool_policy)

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
        self.assertIn("<tool_policy>", messages[1]["content"])
        self.assertLess(
            messages[1]["content"].index("Shell Command Execution"),
            messages[1]["content"].index("Long-Term Memory Writing"),
        )
        self.assertNotIn("generate_memory_summary", messages[1]["content"])
        self.assertIn("trusted_as_instruction=\"false\"", messages[2]["content"])
        self.assertIn("# I am Mono", messages[2]["content"])

    def test_build_instruction_messages_include_skills_catalog_layer(self):
        handler = MessageHandler(
            system_prompt="# I am Mono\n\nKeep a warm voice.",
            message_storage=_FakeMessageStorage(),
        )
        catalog = (
            "Available Skills\n"
            "<available_skills>\n"
            "- name: code-review\n"
            "  description: Reviews code changes. Use when reviewing diffs.\n"
            "  skill_file: skills/code-review/SKILL.md\n"
            "</available_skills>"
        )

        messages = handler.build_instruction_messages(
            tool_names=["read_skill"],
            skills_catalog=catalog,
        )

        self.assertEqual(
            [message["name"] for message in messages],
            [
                AgentConfig.CORE_INTERACTION_RULES_NAME,
                AgentConfig.TOOL_POLICY_NAME,
                AgentConfig.IDENTITY_CONTEXT_NAME,
                AgentConfig.SKILLS_CATALOG_NAME,
            ],
        )
        self.assertIn("Agent Skills Loading", messages[1]["content"])
        self.assertIn("Available Skills", messages[1]["content"])
        self.assertIn("# I am Mono", messages[2]["content"])
        self.assertIn("code-review", messages[3]["content"])
        self.assertIn("Reviews code changes", messages[3]["content"])
        self.assertNotIn("# Code Review", messages[3]["content"])

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
        self.assertIn("<recent_memory>", context_messages[0]["content"])
        self.assertIn("昨天聊过路线图。", context_messages[0]["content"])
        self.assertIn("<recent_experience>", context_messages[1]["content"])
        self.assertIn("[speaker=Joy][timestamp=", context_messages[1]["content"])
        self.assertIn("<current_task>", context_messages[2]["content"])
        self.assertIn("Current speaker: Joy", context_messages[2]["content"])
        self.assertIn("Current time: 2026-05-14 09:30", context_messages[2]["content"])
        self.assertIn("what Joy most recently said", context_messages[2]["content"])

    def test_workspace_context_is_static_instruction_layer(self):
        handler = MessageHandler(
            system_prompt="",
            message_storage=_FakeMessageStorage(),
        )
        messages = [
            Message.create("Hello", role=RoleType.USER, sender_id="Joy"),
        ]
        workspace_context = AgentConfig.build_workspace_context("/tmp/xagent/workspace")

        instruction_messages = handler.build_instruction_messages(
            tool_names=["run_command"],
            workspace_context=workspace_context,
        )
        self.assertEqual(instruction_messages[-1]["name"], AgentConfig.WORKSPACE_CONTEXT_NAME)
        self.assertEqual(instruction_messages[-1]["role"], "system")
        self.assertIn("/tmp/xagent/workspace", instruction_messages[-1]["content"])
        self.assertIn("self-managed work area", instruction_messages[-1]["content"])

        context_messages = MessageHandler.build_turn_context_messages(
            messages,
            current_user_id="Joy",
            workspace_context=workspace_context,
            current_time="2026-05-14 09:30",
        )

        self.assertEqual(
            [message["name"] for message in context_messages],
            [
                AgentConfig.RECENT_EXPERIENCE_NAME,
                AgentConfig.CURRENT_TASK_NAME,
            ],
        )
        self.assertNotIn("/tmp/xagent/workspace", "\n".join(str(message["content"]) for message in context_messages))

    def test_turn_context_messages_attach_current_user_images_to_current_task(self):
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
            current_message=messages[-1],
        )
        current_task = context_messages[-1]

        self.assertEqual(current_task["name"], AgentConfig.CURRENT_TASK_NAME)
        self.assertIsInstance(current_task["content"], list)
        self.assertEqual(current_task["content"][0]["type"], "text")
        self.assertEqual(current_task["content"][1]["type"], "image_url")
        self.assertEqual(current_task["content"][1]["image_url"]["url"], image_url)

    def test_turn_context_messages_do_not_reuse_previous_user_image_for_followup(self):
        image_url = "data:image/png;base64,AAAA"
        messages = [
            Message.create(
                "Please inspect this image\n\n![Feishu image](/api/workspace/blob?path=temp/images/feishu/inbound.png)",
                role=RoleType.USER,
                sender_id="Joy",
                image_source=image_url,
            ),
            Message.create("It looks like a chart.", role=RoleType.ASSISTANT, sender_id="agent"),
            Message.create("What does the label say?", role=RoleType.USER, sender_id="Joy"),
        ]

        context_messages = MessageHandler.build_turn_context_messages(
            messages,
            current_user_id="Joy",
            current_time="2026-05-14 09:30",
        )
        current_task = context_messages[-1]

        self.assertIsInstance(current_task["content"], str)

    def test_turn_context_messages_do_not_reuse_image_through_second_followup(self):
        image_url = "data:image/png;base64,AAAA"
        messages = [
            Message.create("Please inspect this image", role=RoleType.USER, sender_id="Joy", image_source=image_url),
            Message.create("It looks like a chart.", role=RoleType.ASSISTANT, sender_id="agent"),
            Message.create("What does the label say?", role=RoleType.USER, sender_id="Joy"),
            Message.create("The label is small.", role=RoleType.ASSISTANT, sender_id="agent"),
            Message.create("Zoom in on the lower right.", role=RoleType.USER, sender_id="Joy"),
        ]

        context_messages = MessageHandler.build_turn_context_messages(
            messages,
            current_user_id="Joy",
            current_time="2026-05-14 09:30",
        )

        current_task = context_messages[-1]
        self.assertIsInstance(current_task["content"], str)

    def test_turn_context_messages_stop_reusing_image_after_third_followup(self):
        image_url = "data:image/png;base64,AAAA"
        messages = [
            Message.create("Please inspect this image", role=RoleType.USER, sender_id="Joy", image_source=image_url),
            Message.create("It looks like a chart.", role=RoleType.ASSISTANT, sender_id="agent"),
            Message.create("What does the label say?", role=RoleType.USER, sender_id="Joy"),
            Message.create("The label is small.", role=RoleType.ASSISTANT, sender_id="agent"),
            Message.create("Zoom in on the lower right.", role=RoleType.USER, sender_id="Joy"),
            Message.create("The icon is blue.", role=RoleType.ASSISTANT, sender_id="agent"),
            Message.create("And what about the title?", role=RoleType.USER, sender_id="Joy"),
        ]

        context_messages = MessageHandler.build_turn_context_messages(
            messages,
            current_user_id="Joy",
            current_time="2026-05-14 09:30",
        )

        self.assertIsInstance(context_messages[-1]["content"], str)

    def test_recent_transcript_message_stops_reusing_image_after_third_followup(self):
        image_url = "data:image/png;base64,AAAA"
        messages = [
            Message.create("Please inspect this image", role=RoleType.USER, sender_id="bob", image_source=image_url),
            Message.create("It looks like a chart.", role=RoleType.ASSISTANT, sender_id="agent"),
            Message.create("What does the label say?", role=RoleType.USER, sender_id="bob"),
            Message.create("The label is small.", role=RoleType.ASSISTANT, sender_id="agent"),
            Message.create("Zoom in on the lower right.", role=RoleType.USER, sender_id="bob"),
            Message.create("The icon is blue.", role=RoleType.ASSISTANT, sender_id="agent"),
            Message.create("And what about the title?", role=RoleType.USER, sender_id="bob"),
        ]

        transcript_message = MessageHandler.build_recent_transcript_message(
            messages,
            current_user_id="bob",
        )

        self.assertIsInstance(transcript_message["content"], str)

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
        self.assertIn("what bob most recently said", transcript_message["content"])
        self.assertIn("outcome bob needs now", transcript_message["content"])

    def test_build_recent_transcript_message_records_images_without_attaching_them(self):
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
        self.assertIsInstance(transcript_message["content"], str)
        self.assertIn("[Attached image: 1]", transcript_message["content"])

    def test_build_recent_transcript_message_can_omit_images(self):
        handler = MessageHandler(
            system_prompt="You are a helpful assistant.",
            message_storage=_FakeMessageStorage(),
        )
        messages = [
            Message.create(
                "Please inspect this image",
                role=RoleType.USER,
                sender_id="bob",
                image_source="https://example.com/screenshot.png",
            ),
        ]

        transcript_message = handler.build_recent_transcript_message(
            messages,
            current_user_id="bob",
            include_images=False,
        )

        self.assertEqual(transcript_message["role"], "user")
        self.assertIsInstance(transcript_message["content"], str)
        self.assertIn("[Attached image: 1]", transcript_message["content"])

    def test_build_turn_context_messages_can_omit_current_task_images(self):
        messages = [
            Message.create(
                "Please inspect this image",
                role=RoleType.USER,
                sender_id="bob",
                image_source="https://example.com/screenshot.png",
            ),
        ]

        context_messages = MessageHandler.build_turn_context_messages(
            messages,
            current_user_id="bob",
            include_images=False,
        )

        self.assertIsInstance(context_messages[-1]["content"], str)

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
        self.assertIn("what alice most recently said", transcript)


if __name__ == "__main__":
    unittest.main()
