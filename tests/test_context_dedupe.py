"""Regression tests for one-event-one-appearance context architecture (Phase 1)."""

import unittest
from datetime import datetime

from xagent.core.config import AgentConfig
from xagent.core.formatters import RoomContextEntry, RoomSnapshot
from xagent.core.handlers.message import MessageHandler
from xagent.core.inbox import INBOX_KIND_METADATA_KEY, InboxKind
from xagent.schemas import Message, RoleType, MessageType


def _flatten_turn_text(messages: list[dict]) -> str:
    parts: list[str] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
    return "\n".join(parts)


class ContextDedupeTests(unittest.TestCase):
    def test_last_input_message_is_current_input(self):
        current = Message.create("answer this", role=RoleType.USER, sender_id="Joy")
        messages = MessageHandler.build_turn_context_messages(
            [current],
            current_user_id="Joy",
            current_message=current,
        )
        self.assertEqual(messages[-1]["name"], AgentConfig.CURRENT_INPUT_NAME)

    def test_current_input_contains_body_for_user_presence_and_scheduled(self):
        user = Message.create("user line", role=RoleType.USER, sender_id="Joy")
        user_messages = MessageHandler.build_turn_context_messages(
            [user],
            current_user_id="Joy",
            current_message=user,
        )
        user_input = next(m["content"] for m in user_messages if m["name"] == AgentConfig.CURRENT_INPUT_NAME)
        self.assertIn("user line", user_input)

        presence = Message.create("hello hall", role=RoleType.USER, sender_id="alice")
        presence.metadata[INBOX_KIND_METADATA_KEY] = InboxKind.PRESENCE_TURN.value
        presence_messages = MessageHandler.build_turn_context_messages(
            [presence],
            current_user_id="alice",
            current_message=presence,
            room_context="[room context]\nroom_id: plaza\n\nalice: hello hall\n[/room context]",
        )
        presence_input = next(
            m["content"] for m in presence_messages if m["name"] == AgentConfig.CURRENT_INPUT_NAME
        )
        self.assertIn("hello hall", presence_input)
        self.assertIn('kind="presence_turn"', presence_input)

        scheduled = Message.create("wrapper", role=RoleType.USER, sender_id="Joy")
        scheduled.metadata[INBOX_KIND_METADATA_KEY] = InboxKind.SCHEDULED_TURN.value
        scheduled.metadata["task_content"] = "ping task"
        scheduled_messages = MessageHandler.build_turn_context_messages(
            [scheduled],
            current_user_id="Joy",
            current_message=scheduled,
        )
        scheduled_input = next(
            m["content"] for m in scheduled_messages if m["name"] == AgentConfig.CURRENT_INPUT_NAME
        )
        self.assertIn("ping task", scheduled_input)

    def test_channel_instructions_rendered_as_system_policy(self):
        from xagent.core.handlers.message import MessageHandler as Handler

        handler = Handler(message_storage=object())
        instructions = handler.build_instruction_messages(
            channel_instructions='Use <at user_id="ou_x">Name</at>.',
        )
        policy = next(m for m in instructions if m["name"] == AgentConfig.CHANNEL_POLICY_NAME)
        self.assertEqual(policy["role"], "system")
        self.assertIn("<channel_policy", policy["content"])

    def test_room_snapshot_covered_entries_excluded_from_recent_experience(self):
        event_id = "feishu:om_1"
        line = Message.create("anyone here?", role=RoleType.USER, sender_id="alice")
        line.source_event_id = event_id
        line.room_id = "oc_group"
        snapshot = RoomSnapshot(
            room_id="oc_group",
            room_name="",
            entries=(
                RoomContextEntry(
                    speaker_label="alice(ou_alice)",
                    occurred_at=datetime(2026, 5, 14, 9, 30),
                    text="anyone here?",
                    event_id=event_id,
                ),
            ),
        )
        messages = MessageHandler.build_turn_context_messages(
            [line],
            current_user_id="alice",
            current_message=line,
            room_context=snapshot,
        )
        by_name = {m["name"]: m["content"] for m in messages}
        self.assertIn("anyone here?", by_name[AgentConfig.ROOM_CONTEXT_NAME])
        self.assertNotIn("anyone here?", by_name.get(AgentConfig.RECENT_EXPERIENCE_NAME, ""))

    def test_legacy_string_room_context_still_dedupes_trigger(self):
        current = Message.create("trigger line", role=RoleType.USER, sender_id="bob")
        current.source_event_id = "feishu:om_trigger"
        current.room_id = "oc_group"
        room_block = (
            "[room context]\n"
            "room_id: oc_group\n\n"
            "bob(ou_bob) 2026-05-14 09:30: trigger line\n"
            "[/room context]"
        )
        messages = MessageHandler.build_turn_context_messages(
            [current],
            current_user_id="bob",
            current_message=current,
            room_context=room_block,
        )
        text = _flatten_turn_text(messages)
        self.assertEqual(text.count("trigger line"), 2)

    def test_presence_turn_event_appears_exactly_once_in_turn_layers(self):
        overheard = Message.create("anyone here?", role=RoleType.USER, sender_id="alice")
        overheard.source_event_id = "world:plaza:99"
        overheard.room_id = "plaza"
        overheard.metadata[INBOX_KIND_METADATA_KEY] = InboxKind.PRESENCE_TURN.value
        observation = Message(
            content="anyone here?",
            role=RoleType.ENVIRONMENT,
            type=MessageType.CONTEXT_EVENT,
            sender_id="alice",
            source_event_id="world:plaza:99",
            room_id="plaza",
        )
        snapshot = RoomSnapshot(
            room_id="plaza",
            room_name="大厅",
            entries=(
                RoomContextEntry(
                    speaker_label="alice",
                    occurred_at=datetime(2026, 5, 14, 9, 30),
                    text="anyone here?",
                    event_id="world:plaza:99",
                ),
            ),
        )
        messages = MessageHandler.build_turn_context_messages(
            [observation, overheard],
            current_user_id="alice",
            current_message=overheard,
            room_context=snapshot,
        )
        text = _flatten_turn_text(messages)
        self.assertEqual(text.count("anyone here?"), 2)
        self.assertLessEqual(text.lower().count("alice"), 3)

    def test_observation_content_has_no_speaker_prefix(self):
        observation = Message(
            content="plain observation body",
            role=RoleType.ENVIRONMENT,
            type=MessageType.CONTEXT_EVENT,
            sender_id="alice",
            metadata={"sender_name": "Alice"},
        )
        messages = MessageHandler.build_turn_context_messages(
            [observation],
            current_user_id="Joy",
        )
        experience = next(m["content"] for m in messages if m["name"] == AgentConfig.RECENT_EXPERIENCE_NAME)
        self.assertIn("plain observation body", experience)
        self.assertNotIn("Alice: plain observation body", experience)


if __name__ == "__main__":
    unittest.main()
