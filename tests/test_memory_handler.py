"""Tests for MemoryHandler (diary context + background write scheduling)."""

import asyncio
import tempfile
import unittest
from datetime import date, timedelta

from xagent.components.memory import ExperienceMemoryStore
from xagent.core.handlers.memory import MemoryHandler
from xagent.schemas.memory import MemoryFact, MemorySynthesis, PeopleProfileFact, PeopleProfileUpdates


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
        self.memory = ExperienceMemoryStore(f"{self._tmpdir.name}/xagent_memory.sqlite3")
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
        await self.memory.remember("Today's diary entry", kind="episodic", observed_at=today)
        ctx = await self.handler.get_recent_context(days=1)
        self.assertIn("Today's diary entry", ctx)

    async def test_get_recent_context_is_prompt_brief_and_honors_days(self):
        today = date.today()
        await self.memory.remember(
            "I prefer quiet implementation plans.",
            kind="preference",
            subject_type="self",
            subject_key="self",
            observed_at=today - timedelta(days=30),
        )
        await self.memory.remember(
            "Recent memory architecture discussion.",
            kind="episodic",
            observed_at=today,
        )
        await self.memory.remember(
            "Old diary entry outside the requested window.",
            kind="episodic",
            observed_at=today - timedelta(days=3),
        )

        ctx = await self.handler.get_recent_context(days=1)

        self.assertIn("Durable facts:", ctx)
        self.assertIn("Recent episodes (1d):", ctx)
        self.assertIn("I prefer quiet implementation plans.", ctx)
        self.assertIn("Recent memory architecture discussion.", ctx)
        self.assertNotIn("Old diary entry outside the requested window.", ctx)
        self.assertNotIn("memory_id", ctx)

    async def test_schedule_diary_write_threshold_trigger(self):
        """Messages accumulate and trigger when threshold + interval are met."""
        self.handler._last_write_time = 0.0  # Ensure interval is met
        msgs = [{"role": "user", "content": f"msg {i}"} for i in range(self.handler.message_threshold)]
        self.handler.schedule_diary_write(msgs)
        await asyncio.sleep(0.5)
        for task in list(self.handler._background_tasks):
            await task

        recalled = await self.memory.recall_memory("msg")
        self.assertIn("msg", recalled["items"][0]["content"])

    async def test_schedule_diary_write_no_trigger_below_threshold(self):
        """Below threshold and no explicit trigger — nothing is written."""
        self.handler.schedule_diary_write([
            {"role": "user", "content": "just a chat"}
        ])
        await asyncio.sleep(0.2)
        recalled = await self.memory.recall_memory("just a chat")
        self.assertEqual(recalled["items"], [])

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

        recalled = await self.memory.recall_memory("short important")
        self.assertIn("short but important", recalled["items"][0]["content"])

    async def test_flush_pending_writes_below_threshold_immediately(self):
        self.handler.schedule_diary_write([
            {"role": "user", "content": "save me before exit"}
        ])

        await self.handler.flush_pending()

        recalled = await self.memory.recall_memory("save before exit")
        self.assertIn("save me before exit", recalled["items"][0]["content"])

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
        recalled = await self.memory.recall_memory("first second")
        self.assertIn("first", recalled["items"][0]["content"])
        self.assertIn("second", recalled["items"][0]["content"])

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

        profile_rows = await self.memory.recall_memory("concise implementation", subject_key="Alice", include_evidence=True)
        self.assertIn("Alice prefers concise implementation plans.", profile_rows["items"][0]["content"])
        self.assertIn("I prefer concise implementation plans.", profile_rows["items"][0]["evidence"][0]["quote"])

    async def test_people_profile_failure_does_not_block_diary(self):
        class BrokenProfileLLM(_FakeLLMService):
            async def extract_people_profile_updates(self, messages, diary_entry, journal_date):
                raise RuntimeError("profile extraction failed")

        handler = MemoryHandler(memory=self.memory, llm_service=BrokenProfileLLM())
        handler.schedule_diary_write([
            {"role": "user", "sender_id": "Alice", "content": "write the diary anyway"}
        ])

        await handler.flush_pending()

        recalled = await self.memory.recall_memory("write diary anyway")
        self.assertIn("write the diary anyway", recalled["items"][0]["content"])

    async def test_synthesis_path_writes_episode_and_durable_facts(self):
        class SynthesisLLM(_FakeLLMService):
            async def synthesize_memory(self, messages, journal_date):
                return MemorySynthesis(
                    experience_summary="I refined the memory design.",
                    facts=[
                        MemoryFact(
                            kind="preference",
                            subject_type="self",
                            subject_key="self",
                            title="Elegant memory",
                            content="I want memory interactions to feel elegant.",
                            evidence="一定要优雅",
                            source="direct request",
                            confidence=0.95,
                            salience=0.9,
                        )
                    ],
                )

        handler = MemoryHandler(memory=self.memory, llm_service=SynthesisLLM())
        handler.schedule_diary_write([
            {"role": "user", "sender_id": "Z", "content": "一定要优雅"}
        ])

        await handler.flush_pending()

        episode_rows = await self.memory.recall_memory("refined memory design", kinds="episodic")
        fact_rows = await self.memory.recall_memory("memory interactions elegant", kinds="preference")
        self.assertIn("I refined the memory design.", episode_rows["items"][0]["content"])
        self.assertIn("I want memory interactions to feel elegant.", fact_rows["items"][0]["content"])

    async def test_generate_previous_weekly_summary_if_missing_writes_summary(self):
        today = date(2026, 5, 18)  # Monday
        week_start = date(2026, 5, 11)
        week_end = date(2026, 5, 17)
        await self.memory.remember("Previous week memory", kind="episodic", observed_at=week_start)

        generated = await self.handler.generate_previous_weekly_summary_if_missing(today=today)

        self.assertTrue(generated)
        summary = await self.memory.query_sql("SELECT content FROM memory_summaries WHERE summary_type = 'weekly'")
        self.assertIn("[Summary: weekly 2026-05-11 to 2026-05-17]", summary["rows"][0]["content"])

    async def test_generate_previous_weekly_summary_if_missing_skips_existing_summary(self):
        today = date(2026, 5, 18)  # Monday
        week_start = date(2026, 5, 11)
        week_end = date(2026, 5, 17)
        await self.memory.remember("Previous week memory", kind="episodic", observed_at=week_start)
        await self.memory.add_summary(
            summary_type="weekly",
            scope_type="self",
            scope_key="self",
            period_start=week_start,
            period_end=week_end,
            content="Existing",
        )

        generated = await self.handler.generate_previous_weekly_summary_if_missing(today=today)

        self.assertFalse(generated)
        self.assertEqual(self.llm.summary_calls, [])
        summary = await self.memory.query_sql("SELECT content FROM memory_summaries WHERE summary_type = 'weekly'")
        self.assertEqual(summary["rows"][0]["content"], "Existing")

    async def test_generate_previous_weekly_summary_if_missing_skips_empty_source(self):
        today = date(2026, 5, 18)  # Monday
        week_start = date(2026, 5, 11)
        week_end = date(2026, 5, 17)

        generated = await self.handler.generate_previous_weekly_summary_if_missing(today=today)

        self.assertFalse(generated)
        self.assertFalse(
            await self.memory.summary_exists(
                summary_type="weekly",
                period_start=week_start,
                period_end=week_end,
            )
        )


if __name__ == "__main__":
    unittest.main()
