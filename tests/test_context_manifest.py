"""Tests for context manifest assembly (Phase 2)."""

import json
import unittest

from xagent.core.config import AgentConfig
from xagent.core.context_budget import content_char_length
from xagent.core.context_manifest import build_context_manifest
from xagent.core.handlers.message import MessageHandler
from xagent.schemas import Message, RoleType


class _FakeStorage:
    path = "/tmp/fake.sqlite3"

    async def add_messages(self, *messages):
        return None


class ContextManifestTests(unittest.TestCase):
    def test_manifest_covers_every_assembled_section(self):
        handler = MessageHandler(message_storage=_FakeStorage(), system_prompt="I am Mono.")
        current = Message.create("hello", role=RoleType.USER, sender_id="Joy")
        instructions, instruction_entries = handler.build_instruction_messages_with_manifest(
            tool_names=["write_memory"],
            channel_instructions="mention syntax",
        )
        turn_messages, turn_entries = MessageHandler.build_turn_context_with_manifest(
            [current],
            current_user_id="Joy",
            current_message=current,
            memory_context="## diary\nnote",
            prompt_registry=handler.prompt_registry,
        )
        manifest = build_context_manifest(
            turn_id="t1",
            task_mode="reply",
            inbox_kind="user_turn",
            instruction_entries=instruction_entries,
            turn_entries=turn_entries,
            tool_specs=[{"name": "write_memory"}],
            provider_messages=[*instructions, *turn_messages],
        )
        assembled_names = {message["name"] for message in [*instructions, *turn_messages]}
        manifest_names = {entry.name for entry in manifest.entries}
        self.assertTrue(assembled_names.issubset(manifest_names))
        self.assertGreater(manifest.tools_count, 0)

    def test_manifest_totals_match_compiled_request(self):
        handler = MessageHandler(message_storage=_FakeStorage())
        current = Message.create("payload", role=RoleType.USER, sender_id="Joy")
        instructions, instruction_entries = handler.build_instruction_messages_with_manifest()
        turn_messages, turn_entries = MessageHandler.build_turn_context_with_manifest(
            [current],
            current_user_id="Joy",
            current_message=current,
            prompt_registry=handler.prompt_registry,
        )
        tool_specs = [{"type": "function", "function": {"name": "noop"}}]
        manifest = build_context_manifest(
            turn_id="t2",
            task_mode="reply",
            inbox_kind="",
            instruction_entries=instruction_entries,
            turn_entries=turn_entries,
            tool_specs=tool_specs,
            provider_messages=[*instructions, *turn_messages],
        )
        expected_chars = sum(
            content_char_length(message.get("content"))
            for message in [*instructions, *turn_messages]
        ) + len(json.dumps(tool_specs, ensure_ascii=False, separators=(",", ":")))
        self.assertEqual(manifest.total_chars, expected_chars)
        current_entry = next(
            entry for entry in manifest.entries if entry.name == AgentConfig.CURRENT_INPUT_NAME
        )
        self.assertEqual(current_entry.priority, "required")
        self.assertEqual(current_entry.authority, "task")


if __name__ == "__main__":
    unittest.main()
