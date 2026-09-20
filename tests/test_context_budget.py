"""Tests for global context budget (Phase 3)."""

import json
import unittest
from datetime import datetime, timedelta

from xagent.core.config import AgentConfig
from xagent.core.context_budget import (
    apply_context_budget,
    estimate_tokens,
    fold_tool_outputs,
    truncate_middle,
    trim_experience_entries,
)
from xagent.core.context_manifest import ManifestEntry, manifest_entry_from_message
from xagent.core.handlers.memory import MemoryHandler
from xagent.core.handlers.message import MessageHandler
from xagent.core.formatters import RoomContextEntry, RoomSnapshot
from xagent.schemas import Message, RoleType


class ContextBudgetTests(unittest.TestCase):
    def _room_turn(self, bodies, *, memory_context=""):
        rows = []
        entries = []
        for index, body in enumerate(bodies):
            row = Message.create(body, role=RoleType.USER, sender_id=f"user{index}")
            row.room_id = "oc_group"
            row.source_event_id = f"feishu:om_{index}"
            rows.append(row)
            entries.append(RoomContextEntry(
                speaker_label=f"user{index}",
                occurred_at=datetime(2026, 9, 20, 10) + timedelta(minutes=index),
                text=body,
                event_id=row.source_event_id,
            ))
        return MessageHandler.build_turn_context_with_manifest(
            rows,
            current_user_id=rows[-1].sender_id,
            current_message=rows[-1],
            room_context=RoomSnapshot("oc_group", "测试群", tuple(entries)),
            memory_context=memory_context,
        )

    def test_budget_keeps_deduped_group_speech_before_old_memory(self):
        speech = "项目周五上线"
        messages, entries = self._room_turn(
            [speech, "刚才说了什么？"], memory_context="旧日记。" * 1000,
        )
        experience = next(m["content"] for m in messages if m["name"] == AgentConfig.RECENT_EXPERIENCE_NAME)
        self.assertNotIn(speech, experience)
        _, output, _, manifest, reason = apply_context_budget(
            [], messages, [], entries, [], budget_tokens=1000,
        )
        text = "\n".join(m["content"] for m in output)
        self.assertEqual(text.count(speech), 1)
        self.assertNotIn("旧日记", text)
        room = next(e for e in manifest if e.name == AgentConfig.ROOM_CONTEXT_NAME)
        self.assertFalse(room.dropped)
        self.assertEqual(reason, "")

    def test_room_budget_drops_old_entries_and_keeps_attribution(self):
        bodies = [f"旧消息{i}" + "较早的讨论。" * 70 for i in range(8)]
        messages, entries = self._room_turn([*bodies, "项目周五上线", "刚才说了什么？"])
        _, output, _, manifest, reason = apply_context_budget(
            [], messages, [], entries, [], budget_tokens=650,
        )
        room = next(m["content"] for m in output if m["name"] == AgentConfig.ROOM_CONTEXT_NAME)
        self.assertIn("room_name: 测试群", room)
        self.assertIn("room_id: oc_group", room)
        self.assertIn("user8 2026-09-20 10:08: 项目周五上线", room)
        self.assertNotIn("刚才说了什么？", room)
        self.assertEqual(str(output).count("刚才说了什么？"), 1)
        self.assertNotIn("旧消息0", room)
        self.assertIn("[Earlier room messages omitted:", room)
        self.assertTrue(room.endswith("[/room context]"))
        self.assertLessEqual(sum(e.est_tokens for e in manifest) + estimate_tokens("[]"), int(650 * .85))
        self.assertEqual(reason, "")
        self.assertEqual(next(e.reason for e in manifest if e.name == AgentConfig.ROOM_CONTEXT_NAME), "over_budget:room_trimmed")

    def test_room_budget_shortens_long_chinese_bodies_without_losing_speakers(self):
        messages, entries = self._room_turn(["上线计划" + "详细讨论" * 140, "再补充" + "相关内容" * 140, "请概括"])
        _, output, _, manifest, reason = apply_context_budget(
            [], messages, [], entries, [], budget_tokens=400,
        )
        room = next(m["content"] for m in output if m["name"] == AgentConfig.ROOM_CONTEXT_NAME)
        self.assertIn("user1 2026-09-20 10:01: 再补充", room)
        self.assertIn("user0 2026-09-20 10:00: 上线计划", room)
        self.assertNotIn("请概括", room)
        self.assertIn("omitted", room)
        self.assertEqual(reason, "")
        self.assertLessEqual(sum(e.est_tokens for e in manifest) + estimate_tokens("[]"), int(400 * .85))

    def test_room_minimum_overflow_is_reported_instead_of_silent_loss(self):
        messages, entries = self._room_turn(["项目周五上线", "请回答" + "问" * 195])
        _, output, _, manifest, reason = apply_context_budget(
            [], messages, [], entries, [], budget_tokens=256,
        )
        self.assertIn("项目周五上线", str(output))
        self.assertEqual(reason, "continuity_over_budget")
        self.assertFalse(next(e.dropped for e in manifest if e.name == AgentConfig.ROOM_CONTEXT_NAME))

    def test_room_context_unchanged_when_budget_is_sufficient(self):
        messages, entries = self._room_turn(["项目周五上线", "收到"])
        _, output, _, _, reason = apply_context_budget([], messages, [], entries, [], budget_tokens=32000)
        self.assertEqual(output, messages)
        self.assertEqual(reason, "")

    def test_required_sections_never_trimmed(self):
        instructions = [
            {"role": "system", "name": AgentConfig.CORE_INTERACTION_RULES_NAME, "content": "rules"},
            {"role": "system", "name": AgentConfig.IDENTITY_CONTEXT_NAME, "content": "identity"},
        ]
        turn_messages = [
            {
                "role": "user",
                "name": AgentConfig.CURRENT_INPUT_NAME,
                "content": "<current_input>hello</current_input>",
            }
        ]
        instruction_entries = [
            manifest_entry_from_message(
                message,
                kind="instructions",
                trust="policy",
                priority="required",
                authority="core",
            )
            for message in instructions
        ]
        turn_entries = [
            manifest_entry_from_message(
                turn_messages[0],
                kind="turn",
                trust="data",
                priority="required",
                authority="task",
            )
        ]
        out_i, out_t, _, _, reason = apply_context_budget(
            instructions,
            turn_messages,
            instruction_entries,
            turn_entries,
            [],
            budget_tokens=8000,
        )
        self.assertEqual(out_i[0]["name"], AgentConfig.CORE_INTERACTION_RULES_NAME)
        self.assertEqual(out_t[-1]["name"], AgentConfig.CURRENT_INPUT_NAME)
        self.assertNotEqual(reason, "required_over_budget")

    def test_trim_order_optional_before_continuity(self):
        instructions = [
            {"role": "system", "name": AgentConfig.CORE_INTERACTION_RULES_NAME, "content": "x" * 200},
            {"role": "system", "name": AgentConfig.SKILLS_CATALOG_NAME, "content": "skill " * 4000},
        ]
        turn_messages = [
            {"role": "user", "name": AgentConfig.RECENT_MEMORY_NAME, "content": "diary " * 3000},
            {"role": "user", "name": AgentConfig.CURRENT_INPUT_NAME, "content": "task"},
        ]
        instruction_entries = [
            ManifestEntry(
                name=AgentConfig.CORE_INTERACTION_RULES_NAME,
                role="system",
                kind="instructions",
                chars=200,
                est_tokens=50,
                trust="policy",
                priority="required",
                authority="core",
            ),
            ManifestEntry(
                name=AgentConfig.SKILLS_CATALOG_NAME,
                role="system",
                kind="instructions",
                chars=20000,
                est_tokens=5000,
                trust="data",
                priority="optional",
                authority="none",
            ),
        ]
        turn_entries = [
            ManifestEntry(
                name=AgentConfig.RECENT_MEMORY_NAME,
                role="user",
                kind="turn",
                chars=12000,
                est_tokens=3000,
                trust="data",
                priority="continuity",
                authority="none",
            ),
            ManifestEntry(
                name=AgentConfig.CURRENT_INPUT_NAME,
                role="user",
                kind="turn",
                chars=4,
                est_tokens=1,
                trust="data",
                priority="required",
                authority="task",
            ),
        ]
        out_i, out_t, out_ie, _, _ = apply_context_budget(
            instructions,
            turn_messages,
            instruction_entries,
            turn_entries,
            [{"name": "noop"}],
            budget_tokens=900,
        )
        self.assertFalse(any(message.get("name") == AgentConfig.SKILLS_CATALOG_NAME for message in out_i))
        skills_entry = next(entry for entry in out_ie if entry.name == AgentConfig.SKILLS_CATALOG_NAME)
        self.assertTrue(
            "optional" in skills_entry.reason or "dropped" in skills_entry.reason
        )
        self.assertTrue(any(message.get("name") == AgentConfig.CURRENT_INPUT_NAME for message in out_t))

    def test_total_never_exceeds_budget_when_required_fits(self):
        instructions = [
            {"role": "system", "name": AgentConfig.CORE_INTERACTION_RULES_NAME, "content": "short rules"},
        ]
        turn_messages = [
            {"role": "user", "name": AgentConfig.CURRENT_INPUT_NAME, "content": "reply now"},
        ]
        instruction_entries = [
            manifest_entry_from_message(
                instructions[0],
                kind="instructions",
                trust="policy",
                priority="required",
                authority="core",
            )
        ]
        turn_entries = [
            manifest_entry_from_message(
                turn_messages[0],
                kind="turn",
                trust="data",
                priority="required",
                authority="task",
            )
        ]
        _, _, instruction_entries, turn_entries, _ = apply_context_budget(
            instructions,
            turn_messages,
            instruction_entries,
            turn_entries,
            [],
            budget_tokens=500,
        )
        total = sum(entry.est_tokens for entry in (*instruction_entries, *turn_entries))
        effective = int(500 * (1.0 - AgentConfig.CONTEXT_BUDGET_RESERVE_RATIO))
        self.assertLessEqual(total, effective + 5)

    def test_hot_raw_keeps_minimum_entries(self):
        entries = [
            ("message", Message.create(f"m{i}", role=RoleType.USER, sender_id="u"), f"m{i}" * 200)
            for i in range(10)
        ]
        trimmed, _ = trim_experience_entries(
            entries,
            max_est_tokens=200,
            min_conversation_entries=AgentConfig.MIN_HOT_RAW_MESSAGES,
            storage_cursor_fn=lambda _msg: None,
        )
        self.assertGreaterEqual(
            sum(1 for kind, _, _ in trimmed if kind == "message"),
            AgentConfig.MIN_HOT_RAW_MESSAGES,
        )

    def test_first_diary_entry_capped(self):
        huge = "## 2026-01-01 09:00\n" + ("x" * 5000)
        capped = MemoryHandler._cap_diary_entry(huge)
        self.assertLessEqual(len(capped), AgentConfig.MAX_DIARY_ENTRY_CHARS + 40)

    def test_tool_output_folding_preserves_call_result_pairs(self):
        messages = [
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "A" * 5000},
            {"role": "tool", "tool_call_id": "c2", "content": "B" * 5000},
        ]
        folded = fold_tool_outputs(messages, max_total_chars=1000)
        self.assertGreaterEqual(folded, 1)
        self.assertIn("elided", messages[1]["content"])

    def test_unsummarized_dropped_rows_get_omitted_note(self):
        from xagent.core.handlers.message import MessageHandler

        msg = Message.create("old", role=RoleType.USER, sender_id="u")
        msg.metadata[AgentConfig.MESSAGE_STORAGE_CURSOR_KEY] = 50
        entries = [("message", msg, "old line " * 500)]
        trimmed, dropped = trim_experience_entries(
            entries,
            max_est_tokens=8,
            min_conversation_entries=0,
            covers_through_cursor=10,
            storage_cursor_fn=MessageHandler._storage_cursor,
        )
        self.assertEqual(trimmed, [])
        self.assertEqual(dropped, 1)

    def test_truncate_middle_preserves_ends(self):
        text = "START-" + ("m" * 100) + "-END"
        out = truncate_middle(text, 40)
        self.assertTrue(out.startswith("START"))
        self.assertTrue(out.endswith("END"))


if __name__ == "__main__":
    unittest.main()
