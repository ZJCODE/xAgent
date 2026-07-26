"""Tests for MessageHandler system prompt memory injection."""

import base64
from pathlib import Path
import tempfile
import unittest

from xagent.core.config import AgentConfig
from xagent.core.prompts import PromptAssembler
from xagent.core.handlers.message import MessageHandler
from xagent.schemas import Message, MessageType, RoleType
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

            images = msg.images
            self.assertEqual(len(images), 1)
            self.assertTrue(images[0].source.startswith("/api/workspace/blob?path=assets%2Finbound%2Flocal%2Fimages%2F"))
            self.assertEqual(len(msg.metadata["images"]), 1)
            asset = msg.metadata["images"][0]
            self.assertTrue(asset["workspace_path"].startswith("assets/inbound/local/images/"))
            self.assertIn("/api/workspace/blob?path=assets%2Finbound%2Flocal%2Fimages%2F", asset["blob_url"])
            self.assertEqual((Path(tmpdir) / asset["workspace_path"]).read_bytes(), image_bytes)

            current_images = MessageHandler._current_message_images(msg, "Joy", workspace_dir=tmpdir)
            self.assertEqual(data_uri_to_bytes(current_images[0])[0], image_bytes)

    def test_workspace_blob_markdown_is_detected_as_image_input(self):
        blob_url = "/api/workspace/blob?path=assets%2Fgenerated%2Fimages%2Fresult.png"

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
            self.assertTrue(attachment["path"].startswith("assets/inbound/local/images/"))
            self.assertIn("Attached files:", msg.content)
            self.assertIn(attachment["blob_url"], msg.content)
            self.assertIn(f"path: {attachment['path']}", msg.content)
            self.assertEqual((Path(tmpdir) / attachment["path"]).read_bytes(), image_bytes)
            self.assertEqual(storage.messages, [msg])
    
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
                AgentConfig.IDENTITY_CONTEXT_NAME,
            ],
        )
        self.assertEqual([message["role"] for message in messages], ["system", "system"])
        self.assertLessEqual(len(messages[0]["content"]), PromptAssembler.MAX_CORE_CHARS)
        self.assertIn("one life stream across channels", messages[0]["content"])
        self.assertIn("<identity_context>", messages[1]["content"])
        self.assertIn("# I am Mono", messages[1]["content"])

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
                AgentConfig.IDENTITY_CONTEXT_NAME,
                AgentConfig.SKILLS_CATALOG_NAME,
            ],
        )
        self.assertIn("# I am Mono", messages[1]["content"])
        self.assertIn("code-review", messages[2]["content"])
        self.assertIn("Reviews code changes", messages[2]["content"])
        self.assertNotIn("# Code Review", messages[2]["content"])

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
        self.assertIn("speaker=Joy", context_messages[2]["content"])
        self.assertIn("time=2026-05-14 09:30", context_messages[2]["content"])
        self.assertIn("Respond to the latest event", context_messages[2]["content"])
        self.assertLessEqual(
            len(context_messages[2]["content"]),
            PromptAssembler.MAX_CURRENT_TASK_CHARS,
        )

    def test_subconscious_mode_uses_only_experience_and_current_task(self):
        messages = [
            Message.create("Hello", role=RoleType.USER, sender_id="Joy"),
        ]

        context_messages = MessageHandler.build_turn_context_messages(
            messages,
            current_user_id="agent",
            current_time="2026-06-25 18:00",
            task_mode="subconscious_json",
        )

        self.assertEqual(
            [message["name"] for message in context_messages],
            [
                AgentConfig.RECENT_EXPERIENCE_NAME,
                AgentConfig.CURRENT_TASK_NAME,
            ],
        )
        current_task = context_messages[1]
        self.assertNotIn("subconscious_contacts", current_task["content"])
        self.assertIn('mode="subconscious_json"', current_task["content"])
        self.assertNotIn("subconscious_trace", {message["name"] for message in context_messages})

    def test_workspace_context_is_static_instruction_layer(self):
        handler = MessageHandler(
            system_prompt="",
            message_storage=_FakeMessageStorage(),
        )
        messages = [
            Message.create("Hello", role=RoleType.USER, sender_id="Joy"),
        ]
        workspace_context = PromptAssembler.workspace_context("/tmp/xagent/workspace")

        instruction_messages = handler.build_instruction_messages(
            tool_names=["run_command"],
            workspace_context=workspace_context,
        )
        self.assertEqual(instruction_messages[-1]["name"], AgentConfig.WORKSPACE_CONTEXT_NAME)
        self.assertEqual(instruction_messages[-1]["role"], "system")
        self.assertIn("/tmp/xagent/workspace", instruction_messages[-1]["content"])
        self.assertIn("file workspace", instruction_messages[-1]["content"])

        context_messages = MessageHandler.build_turn_context_messages(
            messages,
            current_user_id="Joy",
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
        current_event = next(
            message for message in context_messages
            if message["name"] == AgentConfig.CURRENT_EVENT_NAME
        )

        self.assertIsInstance(current_event["content"], list)
        self.assertEqual(current_event["content"][0]["type"], "text")
        self.assertEqual(current_event["content"][1]["type"], "image_url")
        self.assertEqual(current_event["content"][1]["image_url"]["url"], image_url)

    def test_turn_context_messages_do_not_reuse_previous_user_image_for_followup(self):
        image_url = "data:image/png;base64,AAAA"
        messages = [
            Message.create(
                "Please inspect this image\n\n![Feishu image](/api/workspace/blob?path=assets/inbound/feishu/images/inbound.png)",
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

    def test_long_observation_is_not_truncated_in_recent_experience(self):
        long_observation = "sensor log: " + ("x" * 1800)
        observation = Message.create_context_event(
            long_observation,
            source="sensor",
            event_type="observation",
        )

        context_messages = MessageHandler.build_turn_context_messages(
            [observation],
            current_user_id="alice",
            current_time="2026-06-10 12:00",
        )
        recent_experience = next(
            message["content"]
            for message in context_messages
            if message["name"] == AgentConfig.RECENT_EXPERIENCE_NAME
        )

        self.assertIn(long_observation, recent_experience)
        self.assertNotIn("[Content truncated:", recent_experience)


if __name__ == "__main__":
    unittest.main()
