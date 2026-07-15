"""Tests for MemoryHandler (count-based journaling from MessageStorage)."""

import asyncio
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from xagent.components.memory import MarkdownMemory
from xagent.core.handlers.memory import MemoryHandler
from xagent.schemas import Message, MessageType, RoleType

_TEST_MAX_HISTORY = 20


class _FakeLLMService:
    """Stub that joins message content into a simple diary entry."""

    def __init__(self):
        self.summary_calls = []
        self.diary_calls = []
        self.fail_on_diary_call_number = None
        self.diary_gate = None
        self.diary_started = asyncio.Event()

    async def format_diary_entry(self, messages, journal_date):
        self.diary_calls.append({
            "journal_date": journal_date,
            "messages": list(messages),
        })
        self.diary_started.set()
        if self.diary_gate is not None:
            await self.diary_gate.wait()
        if self.fail_on_diary_call_number is not None and len(self.diary_calls) >= self.fail_on_diary_call_number:
            raise RuntimeError("diary failed")
        return "\n".join(str(message.get("content", "")) for message in messages if message.get("content"))

    async def generate_summary(self, source_content, period_type, period_label):
        self.summary_calls.append((source_content, period_type, period_label))
        return f"[Summary: {period_type} {period_label}]"


class _FakeMessageStorage:
    def __init__(self, messages=None):
        self.messages = list(messages or [])

    async def get_message_count(self):
        return len(self.messages)

    async def get_messages(self, count=20, offset=0):
        if count <= 0:
            return []
        end = len(self.messages) - offset if offset else len(self.messages)
        start = max(0, end - count)
        return self.messages[start:end]

    async def get_latest_message_cursor(self):
        return len(self.messages)

    async def get_messages_in_cursor_range(self, start_exclusive=0, end_inclusive=None):
        start = max(0, int(start_exclusive or 0))
        end = len(self.messages) if end_inclusive is None else max(0, int(end_inclusive))
        if end <= start:
            return []
        return self.messages[start:end]

    async def cursor_for_message_count(self, message_count):
        normalized = max(0, int(message_count or 0))
        return normalized if normalized <= len(self.messages) else 0

    def append(self, message: Message) -> None:
        self.messages.append(message)


class MemoryHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.memory = MarkdownMemory(self._tmpdir.name)
        self.storage = _FakeMessageStorage()
        self.llm = _FakeLLMService()
        self.handler = MemoryHandler(
            memory=self.memory,
            llm_service=self.llm,
            message_storage=self.storage,
            max_history=_TEST_MAX_HISTORY,
        )

    async def asyncTearDown(self):
        maintenance_task = getattr(self.handler, "_maintenance_task", None)
        if maintenance_task is not None and not maintenance_task.done():
            await maintenance_task
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

    async def test_get_recent_context_respects_zero_recent_days(self):
        today = date.today()
        await self.memory.append_daily("Today's diary entry", target_date=today)
        handler = MemoryHandler(
            memory=self.memory,
            llm_service=self.llm,
            message_storage=self.storage,
            max_history=_TEST_MAX_HISTORY,
            recent_days=0,
        )

        ctx = await handler.get_recent_context()
        self.assertEqual(ctx, "")

        ctx_explicit = await handler.get_recent_context(days=0)
        self.assertEqual(ctx_explicit, "")

    async def test_get_recent_context_trims_to_max_chars(self):
        today = date.today()
        yesterday = today - timedelta(days=1)
        await self.memory.append_daily("Y" * 5000, target_date=yesterday)
        await self.memory.append_daily("T" * 5000, target_date=today)
        handler = MemoryHandler(
            memory=self.memory,
            llm_service=self.llm,
            message_storage=self.storage,
            max_history=_TEST_MAX_HISTORY,
            recent_days=2,
            recent_max_chars=6000,
        )

        ctx = await handler.get_recent_context()

        self.assertLessEqual(len(ctx), 6100)
        self.assertIn(today.isoformat(), ctx)
        self.assertIn("[earlier diary omitted within recent window]", ctx)

    async def test_get_subconscious_context_includes_latest_summaries(self):
        today = date.today()
        await self.memory.append_daily("Fresh inner diary note", target_date=today)
        await self.memory.write_summary(
            self.memory.weekly_path(date(2026, 6, 1), date(2026, 6, 7)),
            "Weekly arc about unfinished work.",
        )
        await self.memory.write_summary(
            self.memory.monthly_path(2026, 6),
            "Monthly pattern about relationships.",
        )
        await self.memory.write_summary(
            self.memory.yearly_path(2026),
            "Yearly phase about long-running questions.",
        )

        ctx = await self.handler.get_subconscious_context(days=1)

        self.assertIn("Recent daily diary", ctx)
        self.assertIn("Fresh inner diary note", ctx)
        self.assertIn("Longer-range diary summaries", ctx)
        self.assertIn("Weekly arc about unfinished work", ctx)
        self.assertIn("Monthly pattern about relationships", ctx)
        self.assertIn("Yearly phase about long-running questions", ctx)

    def test_experience_record_preserves_timestamp(self):
        message = Message(
            role=RoleType.ASSISTANT,
            sender_id=None,
            content="I followed up on the task.",
            timestamp=1710000000.0,
        )

        record = MemoryHandler._experience_record(message)

        self.assertEqual(record["timestamp"], 1710000000.0)

    async def test_schedule_experience_write_triggers_background_maintenance(self):
        message = Message(
            role=RoleType.USER,
            sender_id="alice",
            content="write the diary anyway",
            timestamp=1710000000.0,
        )
        self.storage.append(message)
        handler = MemoryHandler(
            memory=self.memory,
            llm_service=self.llm,
            message_storage=self.storage,
            max_history=1,
        )

        handler.schedule_experience_write([message])
        self.assertIsNotNone(handler._maintenance_task)
        await handler._maintenance_task

        today_text = await self.memory.read_file(self.memory.daily_path(date.today()))
        self.assertIn("write the diary anyway", today_text)

    async def test_schedule_experience_write_ignores_routine_context_events(self):
        routine_event = Message(
            role=RoleType.ENVIRONMENT,
            type=MessageType.CONTEXT_EVENT,
            sender_id=None,
            content="heartbeat tick",
            timestamp=1710000000.0,
            metadata={"event_type": "heartbeat", "source": "runtime"},
        )
        self.storage.append(routine_event)

        self.handler.schedule_experience_write([routine_event])

        self.assertIsNone(self.handler._maintenance_task)

    async def test_run_maintenance_skips_when_cursor_gap_below_threshold(self):
        self.storage.append(Message(
            role=RoleType.USER,
            sender_id="alice",
            content="recent note",
            timestamp=1710000000.0,
        ))
        handler = MemoryHandler(
            memory=self.memory,
            llm_service=self.llm,
            message_storage=self.storage,
            max_history=_TEST_MAX_HISTORY,
        )
        # Cursor gap is 0 (last_processed=0, latest=1), below threshold of 14 → no write
        wrote = await handler.run_maintenance(force=False)

        self.assertFalse(wrote)
        self.assertEqual(self.llm.diary_calls, [])

    async def test_run_maintenance_writes_when_cursor_gap_meets_threshold(self):
        # Add 20 messages so cursor gap = 20 >= threshold (max_history - window_overlap = 14)
        for i in range(20):
            self.storage.append(Message(
                role=RoleType.USER,
                sender_id="alice",
                content="quiet reflection note" if i == 0 else f"filler {i}",
                timestamp=1710000000.0 + i,
            ))
        handler = MemoryHandler(
            memory=self.memory,
            llm_service=self.llm,
            message_storage=self.storage,
            max_history=_TEST_MAX_HISTORY,
        )

        wrote = await handler.run_maintenance(force=False)

        self.assertTrue(wrote)
        self.assertEqual(len(self.llm.diary_calls), 1)
        diary_contents = [m["content"] for m in self.llm.diary_calls[0]["messages"]]
        self.assertIn("quiet reflection note", diary_contents)

    async def test_run_maintenance_filters_routine_events_from_compression(self):
        # Add enough filler messages to clear the cursor-gap threshold (14).
        filler_messages = [
            Message(
                role=RoleType.USER,
                sender_id="alice",
                content=f"filler {i}",
                timestamp=1710000000.0 + i,
            )
            for i in range(14)
        ]
        storage = _FakeMessageStorage([
            *filler_messages,
            Message(
                role=RoleType.USER,
                sender_id="alice",
                content="older reflection",
                timestamp=1710000014.0,
            ),
            Message(
                role=RoleType.ENVIRONMENT,
                type=MessageType.CONTEXT_EVENT,
                sender_id=None,
                content="heartbeat tick",
                timestamp=1710000015.0,
                metadata={"event_type": "heartbeat", "source": "runtime"},
            ),
        ])
        handler = MemoryHandler(
            memory=self.memory,
            llm_service=self.llm,
            message_storage=storage,
            max_history=_TEST_MAX_HISTORY,
        )

        wrote = await handler.run_maintenance(force=False)

        self.assertTrue(wrote)
        diary_contents = [m["content"] for m in self.llm.diary_calls[0]["messages"]]
        self.assertIn("older reflection", diary_contents)
        self.assertNotIn("heartbeat tick", diary_contents)

    async def test_run_maintenance_compresses_last_max_history_messages(self):
        stored_messages = [
            Message(
                role=RoleType.USER,
                sender_id="alice",
                content=f"entry {index}",
                timestamp=1710000000 + index,
            )
            for index in range(4)
        ]
        storage = _FakeMessageStorage(stored_messages)
        handler = MemoryHandler(
            memory=self.memory,
            llm_service=self.llm,
            message_storage=storage,
            max_history=_TEST_MAX_HISTORY,
        )

        wrote = await handler.run_maintenance(force=True)

        self.assertTrue(wrote)
        self.assertEqual(len(self.llm.diary_calls), 1)
        self.assertEqual(
            [message["content"] for message in self.llm.diary_calls[0]["messages"]],
            ["entry 0", "entry 1", "entry 2", "entry 3"],
        )
        self.assertEqual(handler._last_processed_message_id, 4)

    async def test_run_maintenance_skips_non_memory_worthy_events_and_advances_checkpoint(self):
        storage = _FakeMessageStorage([
            Message(
                role=RoleType.ENVIRONMENT,
                type=MessageType.CONTEXT_EVENT,
                sender_id=None,
                content="heartbeat tick",
                timestamp=1710000000.0,
                metadata={"event_type": "heartbeat", "source": "runtime"},
            )
        ])
        handler = MemoryHandler(
            memory=self.memory,
            llm_service=self.llm,
            message_storage=storage,
            max_history=_TEST_MAX_HISTORY,
        )

        wrote = await handler.run_maintenance(force=True)

        self.assertFalse(wrote)
        self.assertEqual(handler._last_processed_message_id, 1)
        self.assertEqual(self.llm.diary_calls, [])
        self.assertTrue(Path(self.memory.root / ".journal_cursor").exists())

    async def test_run_maintenance_does_not_advance_checkpoint_when_diary_write_fails(self):
        storage = _FakeMessageStorage([
            Message(
                role=RoleType.USER,
                sender_id="alice",
                content="fragile memory",
                timestamp=1710000000.0,
            )
        ])
        llm = _FakeLLMService()
        llm.fail_on_diary_call_number = 1
        handler = MemoryHandler(
            memory=self.memory,
            llm_service=llm,
            message_storage=storage,
            max_history=_TEST_MAX_HISTORY,
        )

        wrote = await handler.run_maintenance(force=True)

        self.assertFalse(wrote)
        self.assertEqual(handler._last_processed_message_id, 0)
        self.assertFalse(Path(self.memory.root / ".journal_cursor").exists())

    async def test_run_maintenance_retries_full_window_after_partial_batch_failure(self):
        storage = _FakeMessageStorage([
            Message(
                role=RoleType.USER,
                sender_id="alice",
                content=f"entry {index} " + ("x" * 80),
                timestamp=1712000000 + index,
            )
            for index in range(2)
        ])
        failing_llm = _FakeLLMService()
        failing_llm.fail_on_diary_call_number = 2
        handler = MemoryHandler(
            memory=self.memory,
            llm_service=failing_llm,
            message_storage=storage,
            max_history=_TEST_MAX_HISTORY,
            max_journal_source_chars=150,
        )

        first_wrote = await handler.run_maintenance(force=True)

        self.assertFalse(first_wrote)
        self.assertEqual(handler._last_processed_message_id, 0)

        retry_llm = _FakeLLMService()
        retry_handler = MemoryHandler(
            memory=self.memory,
            llm_service=retry_llm,
            message_storage=storage,
            max_history=_TEST_MAX_HISTORY,
            max_journal_source_chars=150,
        )

        second_wrote = await retry_handler.run_maintenance(force=True)

        self.assertTrue(second_wrote)
        self.assertEqual(len(retry_llm.diary_calls), 2)
        today_text = await self.memory.read_file(self.memory.daily_path(date.today()))
        self.assertEqual(today_text.count("entry 0"), 2)
        self.assertEqual(today_text.count("entry 1"), 1)

    async def test_run_maintenance_reads_plain_int_cursor(self):
        storage = _FakeMessageStorage([
            Message(
                role=RoleType.USER,
                sender_id="alice",
                content=f"entry {index}",
                timestamp=1712500000 + index,
            )
            for index in range(4)
        ])
        state_path = self.memory.root / ".journal_cursor"
        state_path.write_text("2", encoding="utf-8")
        handler = MemoryHandler(
            memory=self.memory,
            llm_service=self.llm,
            message_storage=storage,
            max_history=_TEST_MAX_HISTORY,
        )

        wrote = await handler.run_maintenance(force=True)

        self.assertTrue(wrote)
        self.assertEqual(
            [message["content"] for message in self.llm.diary_calls[0]["messages"]],
            ["entry 0", "entry 1", "entry 2", "entry 3"],
        )
        self.assertEqual(state_path.read_text(encoding="utf-8").strip(), "4")

    async def test_run_maintenance_splits_oversized_period_by_source_budget(self):
        storage = _FakeMessageStorage([
            Message(
                role=RoleType.USER,
                sender_id="alice",
                content=f"entry {index} " + ("x" * 80),
                timestamp=1711000000 + index,
            )
            for index in range(3)
        ])
        handler = MemoryHandler(
            memory=self.memory,
            llm_service=self.llm,
            message_storage=storage,
            max_history=_TEST_MAX_HISTORY,
            max_journal_source_chars=150,
        )

        wrote = await handler.run_maintenance(force=True)

        self.assertTrue(wrote)
        self.assertEqual(len(self.llm.diary_calls), 3)
        self.assertEqual(
            [call["messages"][0]["content"] for call in self.llm.diary_calls],
            [f"entry {index} " + ("x" * 80) for index in range(3)],
        )

    async def test_run_maintenance_reloads_checkpoint_from_disk_for_stale_handler(self):
        storage = _FakeMessageStorage([
            Message(
                role=RoleType.USER,
                sender_id="alice",
                content=f"entry {index}",
                timestamp=1712600000 + index,
            )
            for index in range(2)
        ])
        first_llm = _FakeLLMService()
        first_handler = MemoryHandler(
            memory=self.memory,
            llm_service=first_llm,
            message_storage=storage,
            max_history=_TEST_MAX_HISTORY,
        )
        stale_llm = _FakeLLMService()
        stale_handler = MemoryHandler(
            memory=self.memory,
            llm_service=stale_llm,
            message_storage=storage,
            max_history=_TEST_MAX_HISTORY,
        )

        first_wrote = await first_handler.run_maintenance(force=True)
        second_wrote = await stale_handler.run_maintenance(force=True)

        self.assertTrue(first_wrote)
        self.assertFalse(second_wrote)
        self.assertEqual(stale_llm.diary_calls, [])

    async def test_force_maintenance_does_not_replay_overlap_without_new_messages(self):
        storage = _FakeMessageStorage([
            Message(
                role=RoleType.USER,
                sender_id="alice",
                content=f"entry {index}",
                timestamp=1712650000 + index,
            )
            for index in range(8)
        ])
        handler = MemoryHandler(
            memory=self.memory,
            llm_service=self.llm,
            message_storage=storage,
            max_history=_TEST_MAX_HISTORY,
        )

        first_wrote = await handler.run_maintenance(
            force=True,
            trigger="idle",
            idle_seconds=1800,
        )
        second_wrote = await handler.run_maintenance(
            force=True,
            trigger="idle",
            idle_seconds=2100,
        )

        self.assertTrue(first_wrote)
        self.assertFalse(second_wrote)
        self.assertEqual(len(self.llm.diary_calls), 1)
        self.assertEqual(handler._last_processed_message_id, 8)

    async def test_run_maintenance_serializes_handlers_with_workspace_lock(self):
        storage = _FakeMessageStorage([
            Message(
                role=RoleType.USER,
                sender_id="alice",
                content="shared window",
                timestamp=1712700000.0,
            )
        ])
        blocking_llm = _FakeLLMService()
        blocking_llm.diary_gate = asyncio.Event()
        waiting_llm = _FakeLLMService()
        first_handler = MemoryHandler(
            memory=self.memory,
            llm_service=blocking_llm,
            message_storage=storage,
            max_history=_TEST_MAX_HISTORY,
        )
        second_handler = MemoryHandler(
            memory=self.memory,
            llm_service=waiting_llm,
            message_storage=storage,
            max_history=_TEST_MAX_HISTORY,
        )

        first_task = asyncio.create_task(first_handler.run_maintenance(force=True))
        await asyncio.wait_for(blocking_llm.diary_started.wait(), timeout=1)
        second_task = asyncio.create_task(second_handler.run_maintenance(force=True))

        await asyncio.sleep(0.05)
        self.assertFalse(second_task.done())

        blocking_llm.diary_gate.set()
        first_result, second_result = await asyncio.gather(first_task, second_task)

        self.assertTrue(first_result)
        self.assertFalse(second_result)
        self.assertEqual(len(blocking_llm.diary_calls), 1)
        self.assertEqual(waiting_llm.diary_calls, [])

    async def test_run_maintenance_recovers_checkpoint_after_restart(self):
        storage = _FakeMessageStorage([
            Message(
                role=RoleType.USER,
                sender_id="alice",
                content=f"old {index}",
                timestamp=1713000000 + index,
            )
            for index in range(2)
        ])
        first_handler = MemoryHandler(
            memory=self.memory,
            llm_service=self.llm,
            message_storage=storage,
            max_history=_TEST_MAX_HISTORY,
        )

        first_wrote = await first_handler.run_maintenance(force=True)
        self.assertTrue(first_wrote)
        self.assertTrue(Path(self.memory.root / ".journal_cursor").exists())

        for index in range(2):
            storage.append(Message(
                role=RoleType.USER,
                sender_id="alice",
                content=f"new {index}",
                timestamp=1713000100 + index,
            ))

        second_llm = _FakeLLMService()
        second_handler = MemoryHandler(
            memory=self.memory,
            llm_service=second_llm,
            message_storage=storage,
            max_history=_TEST_MAX_HISTORY,
        )

        second_wrote = await second_handler.run_maintenance(force=True)

        self.assertTrue(second_wrote)
        self.assertEqual(len(second_llm.diary_calls), 1)
        self.assertEqual(
            [message["content"] for message in second_llm.diary_calls[0]["messages"]],
            ["old 0", "old 1", "new 0", "new 1"],
        )

    async def test_cursor_gap_triggers_maintenance(self):
        # Add exactly max_history (20) messages so cursor gap = 20 >= 14
        for i in range(20):
            self.storage.append(Message(
                role=RoleType.USER,
                sender_id="alice",
                content=f"msg {i}",
                timestamp=1715000000.0 + i,
            ))
        handler = MemoryHandler(
            memory=self.memory,
            llm_service=self.llm,
            message_storage=self.storage,
            max_history=_TEST_MAX_HISTORY,
        )
        # _last_processed_message_id = 0, latest cursor = 20, unprocessed = 20 >= 14

        wrote = await handler.run_maintenance(force=False)

        self.assertTrue(wrote)
        # Checkpoint advanced after successful write
        self.assertGreater(handler._last_processed_message_id, 0)

    async def test_overlap_in_consecutive_compressions(self):
        """Verify consecutive compressions naturally overlap by reading last N."""
        for i in range(30):
            self.storage.append(Message(
                role=RoleType.USER,
                sender_id="alice",
                content=f"msg {i}",
                timestamp=1716000000.0 + i,
            ))
        handler = MemoryHandler(
            memory=self.memory,
            llm_service=self.llm,
            message_storage=self.storage,
            max_history=_TEST_MAX_HISTORY,
        )

        # First compression: cursor-range (0, 20] = msgs 0-19
        wrote1 = await handler.run_maintenance(force=False)
        self.assertTrue(wrote1)
        first_contents = [m["content"] for m in self.llm.diary_calls[0]["messages"]]
        self.assertEqual(len(first_contents), 20)

        # Add enough messages so the cursor gap reaches threshold naturally.
        # After first compression: _last_processed_message_id = 20.
        # New messages 30-43 -> latest = 44, unprocessed = 44-20 = 24 >= 16.
        for i in range(30, 44):
            self.storage.append(Message(
                role=RoleType.USER,
                sender_id="alice",
                content=f"msg {i}",
                timestamp=1716000000.0 + i,
            ))

        # Second compression overlaps with the first batch by the configured
        # window_overlap.
        second_llm = _FakeLLMService()
        handler.llm_service = second_llm
        wrote2 = await handler.run_maintenance(force=False)
        self.assertTrue(wrote2)
        second_contents = [m["content"] for m in second_llm.diary_calls[0]["messages"]]
        self.assertEqual(len(second_contents), 20)

        overlap = handler.window_overlap
        overlap_first = set(first_contents[-overlap:])
        overlap_second = set(second_contents[:overlap])
        self.assertEqual(overlap_first, overlap_second,
                         f"Expected overlap: {sorted(overlap_first)} vs {sorted(overlap_second)}")

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

    async def test_generate_previous_monthly_summary_if_missing_writes_summary(self):
        today = date(2026, 5, 1)
        await self.memory.append_daily("April memory", target_date=date(2026, 4, 15))

        generated = await self.handler.generate_previous_monthly_summary_if_missing(today=today)

        self.assertTrue(generated)
        summary_text = await self.memory.read_file(self.memory.monthly_path(2026, 4))
        self.assertIn("[Summary: monthly 2026-04]", summary_text)

    async def test_generate_previous_yearly_summary_if_missing_writes_summary(self):
        today = date(2026, 1, 1)
        await self.memory.write_summary(self.memory.monthly_path(2025, 12), "December recap")

        generated = await self.handler.generate_previous_yearly_summary_if_missing(today=today)

        self.assertTrue(generated)
        summary_text = await self.memory.read_file(self.memory.yearly_path(2025))
        self.assertIn("[Summary: yearly 2025]", summary_text)


if __name__ == "__main__":
    unittest.main()
