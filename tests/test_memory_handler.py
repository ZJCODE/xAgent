"""Tests for MemoryHandler (diary context + background write scheduling)."""

import asyncio
import tempfile
import unittest
from datetime import date

from xagent.components.memory import MarkdownMemory
from xagent.core.handlers.memory import MemoryHandler
from xagent.schemas.memory import PeopleProfileFact, PeopleProfileUpdates


class _FakeLLMService:
    """Stub that returns the messages joined as a simple entry."""

    def __init__(self):
        self.summary_calls = []

    async def format_diary_entry(self, messages, journal_date):
        parts = [str(m.get("content", "")) for m in messages if m.get("content")]
        return "\n".join(parts)

    async def generate_summary(self, source_content, period_type, period_label):
        self.summary_calls.append((source_content, period_type, period_label))
        return f"[Summary: {period_type} {period_label}]"


class MemoryHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.memory = MarkdownMemory(self._tmpdir.name)
        self.llm = _FakeLLMService()
        self.handler = MemoryHandler(memory=self.memory, llm_service=self.llm)

    async def asyncTearDown(self):
        await self.handler.flush_pending()
        self._tmpdir.cleanup()

    async def test_get_recent_context_empty(self):
        ctx = await self.handler.get_recent_context()
        self.assertEqual(ctx, "")

    async def test_get_recent_context_returns_dailies(self):
        today = date.today()
        await self.memory.append_daily("Today's diary entry", target_date=today)
        ctx = await self.handler.get_recent_context(days=1)
        self.assertIn(today.isoformat(), ctx)
        self.assertIn("Today's diary entry", ctx)

    async def test_schedule_diary_write_threshold_trigger(self):
        """Messages accumulate and trigger when threshold + interval are met."""
        self.handler._last_write_time = 0.0  # Ensure interval is met
        msgs = [{"role": "user", "content": f"msg {i}"} for i in range(self.handler.message_threshold)]
        self.handler.schedule_diary_write(msgs)
        await asyncio.sleep(0.5)
        for task in list(self.handler._background_tasks):
            await task

        today_text = await self.memory.read_file(self.memory.daily_path(date.today()))
        self.assertIn("msg", today_text)

    async def test_schedule_diary_write_no_trigger_below_threshold(self):
        """Below threshold and no explicit trigger — nothing is written."""
        self.handler.schedule_diary_write([
            {"role": "user", "content": "just a chat"}
        ])
        await asyncio.sleep(0.2)
        today_text = await self.memory.read_file(self.memory.daily_path(date.today()))
        self.assertEqual(today_text.strip(), "")

    async def test_schedule_diary_write_empty_messages_ignored(self):
        """Empty message list should not trigger any write."""
        self.handler.schedule_diary_write([])
        self.assertEqual(len(self.handler._background_tasks), 0)

    async def test_stale_timer_flushes_below_threshold(self):
        handler = MemoryHandler(
            memory=self.memory,
            llm_service=self.llm,
            stale_flush_seconds=0.01,
            message_threshold=10,
            min_interval_seconds=300,
        )

        handler.schedule_diary_write([
            {"role": "user", "content": "short but important"}
        ])
        await asyncio.sleep(0.1)
        await handler.flush_pending()

        today_text = await self.memory.read_file(self.memory.daily_path(date.today()))
        self.assertIn("short but important", today_text)

    async def test_flush_pending_writes_below_threshold_immediately(self):
        self.handler.schedule_diary_write([
            {"role": "user", "content": "save me before exit"}
        ])

        await self.handler.flush_pending()

        today_text = await self.memory.read_file(self.memory.daily_path(date.today()))
        self.assertIn("save me before exit", today_text)

    async def test_threshold_flush_cancels_stale_timer(self):
        handler = MemoryHandler(
            memory=self.memory,
            llm_service=self.llm,
            stale_flush_seconds=60,
            message_threshold=2,
            min_interval_seconds=0,
        )
        handler.schedule_diary_write([
            {"role": "user", "content": "first"}
        ])
        self.assertIsNotNone(handler._flush_timer_task)

        handler.schedule_diary_write([
            {"role": "user", "content": "second"}
        ])
        await handler.flush_pending()

        self.assertIsNone(handler._flush_timer_task)
        today_text = await self.memory.read_file(self.memory.daily_path(date.today()))
        self.assertIn("first", today_text)
        self.assertIn("second", today_text)

    async def test_people_profile_updates_after_diary_write(self):
        class ProfileLLM(_FakeLLMService):
            async def extract_people_profile_updates(self, messages, diary_entry, journal_date):
                return PeopleProfileUpdates(updates=[
                    PeopleProfileFact(
                        person_key="Alice",
                        display_name="Alice",
                        fact="Alice prefers concise implementation plans.",
                        evidence="I prefer concise implementation plans.",
                        source="direct message",
                    )
                ])

        handler = MemoryHandler(memory=self.memory, llm_service=ProfileLLM())
        handler.schedule_diary_write([
            {"role": "user", "sender_id": "Alice", "content": "I prefer concise implementation plans."}
        ])

        await handler.flush_pending()

        profile_text = await self.memory.read_file(self.memory.people_path("Alice"))
        self.assertIn("Alice prefers concise implementation plans.", profile_text)
        self.assertIn("I prefer concise implementation plans.", profile_text)

    async def test_people_profile_failure_does_not_block_diary(self):
        class BrokenProfileLLM(_FakeLLMService):
            async def extract_people_profile_updates(self, messages, diary_entry, journal_date):
                raise RuntimeError("profile extraction failed")

        handler = MemoryHandler(memory=self.memory, llm_service=BrokenProfileLLM())
        handler.schedule_diary_write([
            {"role": "user", "sender_id": "Alice", "content": "write the diary anyway"}
        ])

        await handler.flush_pending()

        today_text = await self.memory.read_file(self.memory.daily_path(date.today()))
        self.assertIn("write the diary anyway", today_text)

    async def test_generate_previous_weekly_summary_if_missing_writes_summary(self):
        today = date(2026, 5, 18)  # Monday
        week_start = date(2026, 5, 11)
        week_end = date(2026, 5, 17)
        await self.memory.append_daily("Previous week memory", target_date=week_start)

        generated = await self.handler.generate_previous_weekly_summary_if_missing(today=today)

        self.assertTrue(generated)
        summary_text = await self.memory.read_file(self.memory.weekly_path(week_start, week_end))
        self.assertIn("[Summary: weekly 2026-05-11 to 2026-05-17]", summary_text)

    async def test_generate_previous_weekly_summary_if_missing_skips_existing_summary(self):
        today = date(2026, 5, 18)  # Monday
        week_start = date(2026, 5, 11)
        week_end = date(2026, 5, 17)
        await self.memory.append_daily("Previous week memory", target_date=week_start)
        await self.memory.write_summary(self.memory.weekly_path(week_start, week_end), "Existing")

        generated = await self.handler.generate_previous_weekly_summary_if_missing(today=today)

        self.assertFalse(generated)
        self.assertEqual(self.llm.summary_calls, [])
        summary_text = await self.memory.read_file(self.memory.weekly_path(week_start, week_end))
        self.assertEqual(summary_text, "Existing")

    async def test_generate_previous_weekly_summary_if_missing_skips_empty_source(self):
        today = date(2026, 5, 18)  # Monday
        week_start = date(2026, 5, 11)
        week_end = date(2026, 5, 17)

        generated = await self.handler.generate_previous_weekly_summary_if_missing(today=today)

        self.assertFalse(generated)
        self.assertFalse(self.memory.weekly_path(week_start, week_end).exists())


if __name__ == "__main__":
    unittest.main()
