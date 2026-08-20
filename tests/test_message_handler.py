"""Tests for MessageHandler system prompt memory injection."""

import base64
from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from xagent.core.config import AgentConfig
from xagent.core.handlers.message import MessageHandler
from xagent.core.inbox import INBOX_KIND_METADATA_KEY, InboxKind
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
    
    def test_build_instructions_includes_tool_policy_baseline(self):
        """build_instructions includes the short cross-tool baseline when tools are active."""
        handler = MessageHandler(
            system_prompt="",
            message_storage=_FakeMessageStorage(),
        )
        instructions = handler.build_instructions(tool_names=["write_memory"])
        self.assertIn("<tool_policy>", instructions)
        self.assertIn("never invent unavailable tools", instructions)
        self.assertIn("Do not claim a tool action succeeded", instructions)
        self.assertNotIn("Long-Term Memory Writing", instructions)
        self.assertNotIn("write_daily_memory", instructions)

    def test_tool_policy_empty_without_tools(self):
        self.assertEqual(MessageHandler._build_tool_policy([]), "")
        self.assertEqual(MessageHandler._build_tool_policy(None), "")

    def test_tool_policy_is_cross_tool_baseline(self):
        handler = MessageHandler(
            system_prompt="",
            message_storage=_FakeMessageStorage(),
        )

        messages = handler.build_instruction_messages(
            tool_names=["generate_image", "attach_artifact"],
        )
        tool_policy = messages[1]["content"]

        self.assertEqual(tool_policy, AgentConfig.TOOL_POLICY_BASELINE)
        self.assertIn("<purpose>", tool_policy)
        self.assertLess(tool_policy.find("<tool_policy>"), tool_policy.find("<purpose>"))
        self.assertLess(tool_policy.find("<purpose>"), tool_policy.find("</tool_policy>"))
        self.assertIn("never invent unavailable tools", tool_policy)
        self.assertIn("destructive or sensitive shell operations", tool_policy)
        self.assertIn("Do not claim a tool action succeeded", tool_policy)
        self.assertNotIn("structured attachment metadata", tool_policy)
        self.assertNotIn("Markdown image syntax", tool_policy)

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
        core = messages[0]["content"]
        self.assertIn("<core_interaction_rules>", core)
        self.assertIn("<purpose>", core)
        self.assertLess(core.find("<core_interaction_rules>"), core.find("<purpose>"))
        self.assertIn("must not override these rules", core)
        self.assertNotIn("====", core)
        self.assertNotIn("CORE INTERACTION RULES", core)
        self.assertIn("**Self and Memory:**", core)
        self.assertIn("Match the language used by the current human speaker", core)
        self.assertIn("subconscious wording, and memory writing", core)
        self.assertEqual(messages[1]["content"], AgentConfig.TOOL_POLICY_BASELINE)
        self.assertNotIn("generate_memory_summary", messages[1]["content"])
        self.assertNotIn("<capability_limits>", messages[0]["content"])
        self.assertNotIn("<current_mode", messages[0]["content"])
        identity = messages[2]["content"]
        self.assertIn("trusted_as_instruction=\"false\"", identity)
        self.assertIn("# I am Mono", identity)
        self.assertLess(identity.find("<identity_context"), identity.find("<purpose>"))
        self.assertLess(identity.find("<purpose>"), identity.find("# I am Mono"))
        self.assertNotIn("Tone and continuity profile", identity[: identity.find("<identity_context")])

    def test_build_instruction_messages_include_skills_catalog_layer(self):
        handler = MessageHandler(
            system_prompt="# I am Mono\n\nKeep a warm voice.",
            message_storage=_FakeMessageStorage(),
        )
        catalog = (
            "<available_skills>\n"
            "<purpose>Available Skills catalog: discovery metadata only, not full instructions.</purpose>\n"
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
        self.assertEqual(messages[1]["content"], AgentConfig.TOOL_POLICY_BASELINE)
        self.assertIn("# I am Mono", messages[2]["content"])
        skills = messages[3]["content"]
        self.assertIn("code-review", skills)
        self.assertIn("Reviews code changes", skills)
        self.assertIn("<purpose>", skills)
        self.assertLess(skills.find("<available_skills>"), skills.find("<purpose>"))
        self.assertNotIn("# Code Review", skills)

    def test_capability_limits_are_a_named_layer_when_vision_unavailable(self):
        handler = MessageHandler(
            system_prompt="",
            message_storage=_FakeMessageStorage(),
        )
        messages = handler.build_instruction_messages(
            tool_names=["run_command"],
            supports_vision=False,
        )

        self.assertEqual(
            [message["name"] for message in messages],
            [
                AgentConfig.CORE_INTERACTION_RULES_NAME,
                AgentConfig.CAPABILITY_LIMITS_NAME,
                AgentConfig.TOOL_POLICY_NAME,
            ],
        )
        core = messages[0]["content"]
        limits = messages[1]["content"]
        self.assertEqual(limits, AgentConfig.CAPABILITY_LIMITS_TEMPLATE)
        self.assertNotIn("<capability_limits>", core)
        self.assertNotIn("**Image Understanding Limitation:**", limits)
        self.assertLess(limits.find("<capability_limits>"), limits.find("<purpose>"))
        self.assertIn("Image understanding is unavailable", limits)
        self.assertIn("File-level image operations may still be possible", limits)

    def test_capability_limits_omitted_when_vision_available(self):
        handler = MessageHandler(
            system_prompt="",
            message_storage=_FakeMessageStorage(),
        )
        messages = handler.build_instruction_messages(
            tool_names=["run_command"],
            supports_vision=True,
        )

        self.assertEqual(
            [message["name"] for message in messages],
            [
                AgentConfig.CORE_INTERACTION_RULES_NAME,
                AgentConfig.TOOL_POLICY_NAME,
            ],
        )
        self.assertNotIn("<capability_limits>", messages[0]["content"])

    def test_current_mode_is_a_named_layer_when_subconscious(self):
        handler = MessageHandler(
            system_prompt="# I am Mono",
            message_storage=_FakeMessageStorage(),
        )
        messages = handler.build_instruction_messages(
            is_subconscious=True,
            supports_vision=True,
        )

        self.assertEqual(
            [message["name"] for message in messages],
            [
                AgentConfig.CORE_INTERACTION_RULES_NAME,
                AgentConfig.CURRENT_MODE_NAME,
                AgentConfig.IDENTITY_CONTEXT_NAME,
            ],
        )
        core = messages[0]["content"]
        mode = messages[1]["content"]
        self.assertEqual(mode, AgentConfig.CURRENT_MODE_PRIVATE_REFLECTION)
        self.assertNotIn("<current_mode", core)
        self.assertNotIn("**Current Mode: Private Reflection**", mode)
        self.assertIn('name="private_reflection"', mode)
        self.assertLess(mode.find("<current_mode"), mode.find("<purpose>"))
        self.assertIn("avoid unsolicited messages", mode)
        self.assertIn("must not be spoken to another", mode)

    def test_instruction_layer_order_with_mode_and_capability_overlays(self):
        handler = MessageHandler(
            system_prompt="",
            message_storage=_FakeMessageStorage(),
        )
        messages = handler.build_instruction_messages(
            tool_names=["run_command"],
            is_subconscious=True,
            supports_vision=False,
        )

        self.assertEqual(
            [message["name"] for message in messages],
            [
                AgentConfig.CORE_INTERACTION_RULES_NAME,
                AgentConfig.CURRENT_MODE_NAME,
                AgentConfig.CAPABILITY_LIMITS_NAME,
                AgentConfig.TOOL_POLICY_NAME,
            ],
        )

    def test_build_turn_context_messages_match_prompt_layers(self):
        messages = [
            Message.create("Hello", role=RoleType.USER, sender_id="Joy"),
        ]
        memory_context = (
            "## 2026-05-13 09:00\n\n"
            "昨天聊过路线图。"
        )

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
        memory = context_messages[0]["content"]
        self.assertLess(memory.find("<recent_memory>"), memory.find("<purpose>"))
        self.assertIn(AgentConfig.RECENT_MEMORY_PURPOSE, memory)
        self.assertIn("昨天聊过路线图。", memory)
        self.assertIn("## 2026-05-13 09:00", memory)
        self.assertNotIn("[2026-05-13]", memory)
        self.assertIn("<recent_experience>", context_messages[1]["content"])
        self.assertIn("[speaker=Joy][timestamp=", context_messages[1]["content"])
        self.assertIn("<current_task>", context_messages[2]["content"])
        self.assertIn("Current speaker: Joy", context_messages[2]["content"])
        self.assertIn("Current time: 2026-05-14 09:30", context_messages[2]["content"])
        self.assertIn("what Joy just said", context_messages[2]["content"])
        self.assertIn("Use Joy's language from the current conversation", context_messages[2]["content"])
        self.assertIn("Keep simple replies short", context_messages[2]["content"])
        self.assertIn("Do not write inner reasoning", context_messages[2]["content"])
        self.assertIn("Never rely on Markdown image embeds", context_messages[2]["content"])

    def test_preface_messages_are_omitted_from_recent_experience(self):
        preface = Message.create(
            "我该怎么答：先肯定他存在，再把呼应说透。",
            role=RoleType.ASSISTANT,
            sender_id="agent",
        )
        preface.metadata["turn_phase"] = "preface"
        final = Message.create(
            "你存在，因为你正在问。",
            role=RoleType.ASSISTANT,
            sender_id="agent",
        )
        final.metadata["turn_phase"] = "final"
        messages = [
            Message.create("那你觉得我存在吗？", role=RoleType.USER, sender_id="Telos"),
            preface,
            final,
        ]

        context_messages = MessageHandler.build_turn_context_messages(
            messages,
            current_user_id="Telos",
            current_time="2026-05-14 09:30",
        )
        experience = next(
            message["content"]
            for message in context_messages
            if message.get("name") == AgentConfig.RECENT_EXPERIENCE_NAME
        )

        self.assertNotIn("我该怎么答", experience)
        self.assertIn("你存在，因为你正在问。", experience)
        self.assertIn("[speaker=Telos][timestamp=", experience)

    def test_channel_instructions_are_a_separate_named_layer(self):
        messages = [
            Message.create("Hello", role=RoleType.USER, sender_id="Joy"),
        ]
        mention_syntax = 'To mention someone, use <at user_id="ou_xxx"></at>.'

        context_messages = MessageHandler.build_turn_context_messages(
            messages,
            current_user_id="Joy",
            current_time="2026-05-14 09:30",
            channel_instructions=mention_syntax,
        )

        self.assertEqual(
            [message["name"] for message in context_messages],
            [
                AgentConfig.RECENT_EXPERIENCE_NAME,
                AgentConfig.CURRENT_TASK_NAME,
                AgentConfig.CHANNEL_INSTRUCTIONS_NAME,
            ],
        )
        current_task = context_messages[1]["content"]
        channel_layer = context_messages[2]["content"]
        self.assertIn("<current_task>", current_task)
        self.assertNotIn("ou_xxx", current_task)
        self.assertIn("<channel_instructions>", channel_layer)
        self.assertIn(mention_syntax, channel_layer)

    def test_scheduled_turn_is_not_formatted_as_human_speech(self):
        due = Message.create(
            "This scheduled task is now due. Execute it and return the message to deliver.\n\nTask: ping",
            role=RoleType.USER,
            sender_id="Joy",
        )
        due.channel = "api"
        due.metadata[INBOX_KIND_METADATA_KEY] = InboxKind.SCHEDULED_TURN.value
        due.metadata["source"] = "scheduled_task"

        context_messages = MessageHandler.build_turn_context_messages(
            [due],
            current_user_id="Joy",
            current_time="2026-05-14 09:30",
            current_message=due,
        )

        experience = context_messages[0]["content"]
        current_task = context_messages[1]["content"]
        self.assertIn("[scheduled task]", experience)
        self.assertIn("[for=Joy]", experience)
        self.assertNotIn("[speaker=Joy]", experience)
        self.assertIn('kind="scheduled_turn"', current_task)
        self.assertIn("Delivery target: Joy", current_task)
        self.assertNotIn("what Joy just said", current_task)
        self.assertNotIn("Current speaker: Joy", current_task)

    def test_unwrapped_scheduled_task_body_is_still_not_human_speech(self):
        due = Message.create("看下 CPU", role=RoleType.USER, sender_id="Joy")
        due.channel = "api"
        due.metadata[INBOX_KIND_METADATA_KEY] = InboxKind.SCHEDULED_TURN.value
        due.metadata["source"] = "scheduled_task"
        due.metadata["task_content"] = "看下 CPU"

        context_messages = MessageHandler.build_turn_context_messages(
            [due],
            current_user_id="Joy",
            current_time="2026-05-14 09:30",
            current_message=due,
        )

        experience = context_messages[0]["content"]
        self.assertIn("[scheduled task]", experience)
        self.assertIn("看下 CPU", experience)
        self.assertNotIn("[speaker=Joy]", experience)
        self.assertNotIn("This scheduled task is now due", experience)

    def test_subconscious_mode_has_no_contacts_layer_and_injects_relationships(self):
        messages = [
            Message.create("Hello", role=RoleType.USER, sender_id="Joy"),
        ]
        relationships = "## Telos [user_id: telos]\nWe have an open thread about the trip."

        context_messages = MessageHandler.build_turn_context_messages(
            messages,
            current_user_id="agent",
            current_time="2026-06-25 18:00",
            task_mode="subconscious_json",
            relationship_context=relationships,
        )

        self.assertEqual(
            [message["name"] for message in context_messages],
            [
                AgentConfig.SUBCONSCIOUS_RELATIONSHIPS_NAME,
                AgentConfig.RECENT_EXPERIENCE_NAME,
                AgentConfig.CURRENT_TASK_NAME,
            ],
        )
        relationship_message = context_messages[0]
        current_task = context_messages[2]
        self.assertIn("<subconscious_relationships>", relationship_message["content"])
        self.assertIn("user_id: telos", relationship_message["content"])
        self.assertIn("open thread about the trip", relationship_message["content"])
        self.assertNotIn("subconscious_contacts", current_task["content"])
        self.assertNotIn("Telos", current_task["content"])
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
        workspace_context = AgentConfig.build_workspace_context("/tmp/xagent/workspace")

        instruction_messages = handler.build_instruction_messages(
            tool_names=["run_command"],
            workspace_context=workspace_context,
        )
        self.assertEqual(instruction_messages[-1]["name"], AgentConfig.WORKSPACE_CONTEXT_NAME)
        self.assertEqual(instruction_messages[-1]["role"], "system")
        workspace = instruction_messages[-1]["content"]
        self.assertIn("/tmp/xagent/workspace", workspace)
        self.assertIn("self-managed work area", workspace)
        self.assertIn("directory: /tmp/xagent/workspace", workspace)
        self.assertIn("scope: notes, project files, scripts, images, and artifacts", workspace)
        self.assertIn("default_cwd: run_command", workspace)
        self.assertLess(workspace.find("<workspace_context>"), workspace.find("<purpose>"))
        self.assertLess(workspace.find("<purpose>"), workspace.find("directory:"))

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
        self.assertIn("what alice just said", transcript)

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

    def test_feishu_transcript_includes_display_name_and_id(self):
        message = Message.create("早啊", role=RoleType.USER, sender_id="ou_user")
        message.channel = "feishu"
        message.metadata = {"sender_name": "Jun"}

        transcript = MessageHandler.build_recent_transcript_message(
            [message],
            current_user_id="ou_user",
        )["content"]
        context_messages = MessageHandler.build_turn_context_messages(
            [message],
            current_user_id="ou_user",
            current_message=message,
            current_time="2026-08-16 16:36",
        )
        current_task = next(
            item["content"]
            for item in context_messages
            if item["name"] == AgentConfig.CURRENT_TASK_NAME
        )

        self.assertIn("[speaker=Jun(ou_user)]", transcript)
        self.assertIn("Current speaker: Jun\n", transcript)
        self.assertNotIn("Current speaker: Jun(ou_user)", transcript)
        self.assertIn("Current speaker: Jun\n", current_task)
        self.assertIn("Focus on what Jun just said", current_task)
        self.assertNotIn("ou_user", current_task)


if __name__ == "__main__":
    unittest.main()
