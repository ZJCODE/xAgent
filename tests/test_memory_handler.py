"""Tests for MemoryHandler (time-based journaling from MessageStorage)."""

import asyncio
import json
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

from xagent.components.memory import MarkdownMemory
from xagent.core.handlers.memory import MemoryHandler
from xagent.schemas import Message, MessageType, RoleType


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
            idle_journal_delay_seconds=0,
        )

        handler.schedule_experience_write([message])
        self.assertIsNotNone(handler._maintenance_task)
        await handler._maintenance_task

        today_text = await self.memory.read_file(self.memory.daily_path(date.today()))
        self.assertIn("write the diary anyway", today_text)
        self.assertFalse((self.memory.root / "people").exists())

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

    async def test_run_maintenance_respects_idle_delay(self):
        current_time = time.time()
        self.storage.append(Message(
            role=RoleType.USER,
            sender_id="alice",
            content="recent note",
            timestamp=current_time,
        ))
        handler = MemoryHandler(
            memory=self.memory,
            llm_service=self.llm,
            message_storage=self.storage,
            idle_journal_delay_seconds=1800,
        )

        wrote = await handler.run_maintenance(force=False)

        self.assertFalse(wrote)
        self.assertEqual(self.llm.diary_calls, [])

    async def test_run_maintenance_writes_after_idle_delay(self):
        current_time = time.time()
        self.storage.append(Message(
            role=RoleType.USER,
            sender_id="alice",
            content="quiet reflection note",
            timestamp=current_time - 1900,
        ))
        handler = MemoryHandler(
            memory=self.memory,
            llm_service=self.llm,
            message_storage=self.storage,
            idle_journal_delay_seconds=1800,
        )

        wrote = await handler.run_maintenance(force=False)

        self.assertTrue(wrote)
        self.assertEqual(len(self.llm.diary_calls), 1)
        self.assertEqual(
            [message["content"] for message in self.llm.diary_calls[0]["messages"]],
            ["quiet reflection note"],
        )

    async def test_run_maintenance_ignores_routine_events_when_checking_idle_delay(self):
        current_time = time.time()
        storage = _FakeMessageStorage([
            Message(
                role=RoleType.USER,
                sender_id="alice",
                content="older reflection",
                timestamp=current_time - 1900,
            ),
            Message(
                role=RoleType.ENVIRONMENT,
                type=MessageType.CONTEXT_EVENT,
                sender_id=None,
                content="heartbeat tick",
                timestamp=current_time - 5,
                metadata={"event_type": "heartbeat", "source": "runtime"},
            ),
        ])
        handler = MemoryHandler(
            memory=self.memory,
            llm_service=self.llm,
            message_storage=storage,
            idle_journal_delay_seconds=1800,
        )

        wrote = await handler.run_maintenance(force=False)

        self.assertTrue(wrote)
        self.assertEqual(
            [message["content"] for message in self.llm.diary_calls[0]["messages"]],
            ["older reflection"],
        )

    async def test_run_maintenance_writes_after_max_active_delay(self):
        current_time = time.time()
        storage = _FakeMessageStorage([
            Message(
                role=RoleType.USER,
                sender_id="alice",
                content="phase start",
                timestamp=current_time - 21700,
            ),
            Message(
                role=RoleType.USER,
                sender_id="alice",
                content="still active",
                timestamp=current_time - 30,
            ),
        ])
        handler = MemoryHandler(
            memory=self.memory,
            llm_service=self.llm,
            message_storage=storage,
            idle_journal_delay_seconds=1800,
            max_active_journal_delay_seconds=21600,
        )

        wrote = await handler.run_maintenance(force=False)

        self.assertTrue(wrote)
        self.assertEqual(
            [message["content"] for message in self.llm.diary_calls[0]["messages"]],
            ["phase start", "still active"],
        )

    async def test_run_maintenance_summarizes_all_new_messages_since_checkpoint(self):
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
        )
        (self.memory.root / "journal_state.json").write_text(
            json.dumps({"last_processed_message_id": 1}),
            encoding="utf-8",
        )

        wrote = await handler.run_maintenance(force=True)

        self.assertTrue(wrote)
        self.assertEqual(len(self.llm.diary_calls), 1)
        self.assertEqual(
            [message["content"] for message in self.llm.diary_calls[0]["messages"]],
            ["entry 1", "entry 2", "entry 3"],
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
        )

        wrote = await handler.run_maintenance(force=True)

        self.assertFalse(wrote)
        self.assertEqual(handler._last_processed_message_id, 1)
        self.assertEqual(self.llm.diary_calls, [])
        self.assertTrue(Path(self.memory.root / "journal_state.json").exists())

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
        )

        wrote = await handler.run_maintenance(force=True)

        self.assertFalse(wrote)
        self.assertEqual(handler._last_processed_message_id, 0)
        self.assertFalse(Path(self.memory.root / "journal_state.json").exists())

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
            max_journal_source_chars=150,
        )

        second_wrote = await retry_handler.run_maintenance(force=True)

        self.assertTrue(second_wrote)
        self.assertEqual(len(retry_llm.diary_calls), 2)
        today_text = await self.memory.read_file(self.memory.daily_path(date.today()))
        self.assertEqual(today_text.count("entry 0"), 2)
        self.assertEqual(today_text.count("entry 1"), 1)

    async def test_run_maintenance_migrates_legacy_checkpoint_count(self):
        storage = _FakeMessageStorage([
            Message(
                role=RoleType.USER,
                sender_id="alice",
                content=f"entry {index}",
                timestamp=1712500000 + index,
            )
            for index in range(4)
        ])
        state_path = self.memory.root / "journal_state.json"
        state_path.write_text(
            json.dumps({"last_processed_message_count": 2}),
            encoding="utf-8",
        )
        handler = MemoryHandler(
            memory=self.memory,
            llm_service=self.llm,
            message_storage=storage,
        )

        wrote = await handler.run_maintenance(force=True)

        self.assertTrue(wrote)
        self.assertEqual(
            [message["content"] for message in self.llm.diary_calls[0]["messages"]],
            ["entry 2", "entry 3"],
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["last_processed_message_id"], 4)

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
        )
        stale_llm = _FakeLLMService()
        stale_handler = MemoryHandler(
            memory=self.memory,
            llm_service=stale_llm,
            message_storage=storage,
        )

        first_wrote = await first_handler.run_maintenance(force=True)
        second_wrote = await stale_handler.run_maintenance(force=True)

        self.assertTrue(first_wrote)
        self.assertFalse(second_wrote)
        self.assertEqual(len(stale_llm.diary_calls), 0)

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
        )
        second_handler = MemoryHandler(
            memory=self.memory,
            llm_service=waiting_llm,
            message_storage=storage,
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
        self.assertEqual(len(waiting_llm.diary_calls), 0)

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
        )

        first_wrote = await first_handler.run_maintenance(force=True)
        self.assertTrue(first_wrote)
        self.assertTrue(Path(self.memory.root / "journal_state.json").exists())

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
        )

        second_wrote = await second_handler.run_maintenance(force=True)

        self.assertTrue(second_wrote)
        self.assertEqual(len(second_llm.diary_calls), 1)
        self.assertEqual(
            [message["content"] for message in second_llm.diary_calls[0]["messages"]],
            ["new 0", "new 1"],
        )

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
