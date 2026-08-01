"""Tests for rolling working-context summary."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from xagent.core.config import AgentConfig
from xagent.core.handlers.message import MessageHandler
from xagent.core.working_context import (
    WorkingContextCompactor,
    WorkingContextState,
    WorkingContextStore,
    WorkingContextSummarizer,
)
from xagent.schemas import Message, MessageType, RoleType


class _FakeMessageStorage:
    def __init__(self, messages=None):
        self.messages = list(messages or [])

    async def get_latest_message_cursor(self) -> int:
        return len(self.messages)

    async def get_messages_in_cursor_range(
        self,
        start_exclusive: int = 0,
        end_inclusive: int | None = None,
    ):
        end = len(self.messages) if end_inclusive is None else int(end_inclusive)
        start = max(0, int(start_exclusive))
        return self.messages[start:end]


class _FakeSummarizer:
    def __init__(self):
        self.calls = []

    async def summarize(self, *, previous_summary: str, records):
        self.calls.append(
            {
                "previous_summary": previous_summary,
                "records": list(records),
            }
        )
        speakers = [
            str(record.get("sender_id") or record.get("role"))
            for record in records
        ]
        joined = ",".join(speakers)
        prefix = previous_summary.strip()
        if prefix:
            return f"{prefix}|rolled:{joined}"
        return f"rolled:{joined}"


class WorkingContextStoreTests(unittest.TestCase):
    def test_read_write_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / AgentConfig.WORKING_CONTEXT_FILENAME
            store = WorkingContextStore(path)
            state = WorkingContextState(
                covers_through_cursor=9,
                updated_at=123.0,
                summary="alice agreed on plan B",
            )
            store.write(state)
            loaded = store.read()
            self.assertEqual(loaded.covers_through_cursor, 9)
            self.assertEqual(loaded.summary, "alice agreed on plan B")
            self.assertTrue(path.exists())
            self.assertEqual(path.parent.name, Path(tmpdir).name)


class WorkingContextCompactorTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_roll_until_threshold(self):
        messages = [
            Message.create(f"m-{index}", role=RoleType.USER, sender_id="alice")
            for index in range(15)
        ]
        storage = _FakeMessageStorage(messages)
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WorkingContextStore(Path(tmpdir) / ".working_context.json")
            summarizer = _FakeSummarizer()
            compactor = WorkingContextCompactor(
                store=store,
                message_storage=storage,
                summarizer=summarizer,
                hot_window=12,
                roll_slack=8,
            )
            summary = await compactor.ensure_fresh()
            self.assertEqual(summary, "")
            self.assertEqual(summarizer.calls, [])

    async def test_rolls_and_preserves_summary_across_restart(self):
        messages = [
            Message.create(f"m-{index:02d}", role=RoleType.USER, sender_id="alice")
            for index in range(30)
        ]
        storage = _FakeMessageStorage(messages)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "messages" / AgentConfig.WORKING_CONTEXT_FILENAME
            store = WorkingContextStore(path)
            summarizer = _FakeSummarizer()
            compactor = WorkingContextCompactor(
                store=store,
                message_storage=storage,
                summarizer=summarizer,
                hot_window=12,
                roll_slack=8,
            )
            summary = await compactor.ensure_fresh()
            self.assertTrue(summary.startswith("rolled:"))
            self.assertEqual(len(summarizer.calls), 1)
            # Hot window keeps the newest 12; rolled window is 1..18.
            self.assertEqual(len(summarizer.calls[0]["records"]), 18)
            self.assertEqual(store.read().covers_through_cursor, 18)

            restarted = WorkingContextCompactor(
                store=WorkingContextStore(path),
                message_storage=storage,
                summarizer=_FakeSummarizer(),
                hot_window=12,
                roll_slack=8,
            )
            self.assertEqual(await restarted.current_summary(), summary)

    async def test_summarizer_failure_falls_back_to_existing_summary(self):
        messages = [
            Message.create(f"m-{index:02d}", role=RoleType.USER, sender_id="alice")
            for index in range(30)
        ]
        storage = _FakeMessageStorage(messages)

        class BoomSummarizer:
            async def summarize(self, **kwargs):
                raise RuntimeError("llm down")

        with tempfile.TemporaryDirectory() as tmpdir:
            store = WorkingContextStore(Path(tmpdir) / ".working_context.json")
            store.write(
                WorkingContextState(
                    covers_through_cursor=0,
                    summary="keep me",
                )
            )
            compactor = WorkingContextCompactor(
                store=store,
                message_storage=storage,
                summarizer=BoomSummarizer(),
                hot_window=12,
                roll_slack=8,
            )
            summary = await compactor.ensure_fresh()
            self.assertEqual(summary, "keep me")
            self.assertEqual(store.read().covers_through_cursor, 0)


class WorkingContextPromptInjectionTests(unittest.TestCase):
    def test_working_summary_replaces_omitted_note(self):
        messages = [
            Message.create(f"old-{index:02d}", role=RoleType.USER, sender_id="alice")
            for index in range(20)
        ]
        context = MessageHandler._build_recent_experience_context(
            experience_entries=[
                ("message", messages[-1], messages[-1].content),
            ],
            omitted_messages=19,
            omitted_observations=0,
            working_summary="alice chose plan B; bob still waiting",
        )
        self.assertIn("[Earlier working context]", context)
        self.assertIn("alice chose plan B", context)
        self.assertNotIn("Earlier experience omitted", context)

    def test_omitted_note_remains_without_summary(self):
        messages = [
            Message.create("latest", role=RoleType.USER, sender_id="alice")
        ]
        context = MessageHandler._build_recent_experience_context(
            experience_entries=[
                ("message", messages[0], messages[0].content),
            ],
            omitted_messages=5,
            omitted_observations=1,
            working_summary="",
        )
        self.assertIn("Earlier experience omitted", context)

    def test_summarizer_prompt_is_not_diary(self):
        system_prompt = WorkingContextSummarizer.build_system_prompt()
        self.assertIn("NOT a diary", system_prompt)
        self.assertIn("speaker attribution", system_prompt.lower())
        self.assertNotIn('first-person ("I")', system_prompt)


if __name__ == "__main__":
    unittest.main()
