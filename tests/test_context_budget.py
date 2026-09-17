"""Tests for global context budget (Phase 3)."""

import json
import unittest

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
from xagent.schemas import Message, RoleType


class ContextBudgetTests(unittest.TestCase):
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
