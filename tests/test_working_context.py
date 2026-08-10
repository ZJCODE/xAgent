"""Tests for rolling working-context summary."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from xagent.core.config import AgentConfig
from xagent.core.handlers.message import MessageHandler
from xagent.core.providers import ReasoningConfig
from xagent.core.working_context import (
    WORKING_CONTEXT_FAILURE_COOLDOWN_SECONDS,
    WorkingContextCompactor,
    WorkingContextState,
    WorkingContextStore,
    WorkingContextSummarizer,
)
from xagent.schemas import Message, RoleType


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


class WorkingContextRollSlackTests(unittest.TestCase):
    def test_roll_slack_is_half_hot_window_clamped(self):
        self.assertEqual(AgentConfig.working_context_roll_slack(12), 6)
        self.assertEqual(AgentConfig.working_context_roll_slack(6), 4)   # floor
        self.assertEqual(AgentConfig.working_context_roll_slack(40), 16)  # ceil
        self.assertEqual(AgentConfig.working_context_roll_slack(15), 8)


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
            )
            self.assertEqual(
                compactor.roll_slack,
                AgentConfig.working_context_roll_slack(12),
            )
            view = await compactor.ensure_fresh()
            self.assertEqual(view.summary, "")
            self.assertEqual(view.covers_through_cursor, 0)
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
            )
            view = await compactor.ensure_fresh()
            self.assertTrue(view.summary.startswith("rolled:"))
            self.assertEqual(view.covers_through_cursor, 18)
            self.assertEqual(len(summarizer.calls), 1)
            # Hot window keeps the newest 12; rolled window is 1..18.
            self.assertEqual(len(summarizer.calls[0]["records"]), 18)
            self.assertEqual(store.read().covers_through_cursor, 18)

            restarted = WorkingContextCompactor(
                store=WorkingContextStore(path),
                message_storage=storage,
                summarizer=_FakeSummarizer(),
                hot_window=12,
            )
            self.assertEqual(await restarted.current_summary(), view.summary)

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
            )
            view = await compactor.ensure_fresh()
            self.assertEqual(view.summary, "keep me")
            self.assertEqual(view.covers_through_cursor, 0)
            self.assertEqual(store.read().covers_through_cursor, 0)

    async def test_view_for_turn_returns_consistent_snapshot_while_single_refresh_runs(self):
        messages = [
            Message.create(f"m-{index:02d}", role=RoleType.USER, sender_id="alice")
            for index in range(30)
        ]

        class BlockingSummarizer:
            def __init__(self):
                self.calls = 0
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def summarize(self, **kwargs):
                del kwargs
                self.calls += 1
                self.started.set()
                await self.release.wait()
                return "new summary"

        with tempfile.TemporaryDirectory() as tmpdir:
            store = WorkingContextStore(Path(tmpdir) / ".working_context.json")
            store.write(
                WorkingContextState(
                    covers_through_cursor=0,
                    summary="old summary",
                )
            )
            summarizer = BlockingSummarizer()
            compactor = WorkingContextCompactor(
                store=store,
                message_storage=_FakeMessageStorage(messages),
                summarizer=summarizer,
                hot_window=12,
            )

            first = await compactor.view_for_turn()
            await asyncio.wait_for(summarizer.started.wait(), timeout=1)
            second = await compactor.view_for_turn()

            self.assertEqual(first.summary, "old summary")
            self.assertEqual(first.covers_through_cursor, 0)
            self.assertEqual(second, first)
            self.assertEqual(summarizer.calls, 1)

            summarizer.release.set()
            await compactor.wait_for_refresh()
            refreshed = await compactor.current_view()

            self.assertEqual(refreshed.summary, "new summary")
            self.assertEqual(refreshed.covers_through_cursor, 18)

    async def test_failed_refresh_uses_fixed_cooldown_and_success_clears_it(self):
        messages = [
            Message.create(f"m-{index:02d}", role=RoleType.USER, sender_id="alice")
            for index in range(30)
        ]

        class FlakySummarizer:
            def __init__(self):
                self.calls = 0

            async def summarize(self, **kwargs):
                del kwargs
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("temporary failure")
                return "recovered summary"

        now = [100.0]
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WorkingContextStore(Path(tmpdir) / ".working_context.json")
            store.write(WorkingContextState(summary="keep me"))
            summarizer = FlakySummarizer()
            compactor = WorkingContextCompactor(
                store=store,
                message_storage=_FakeMessageStorage(messages),
                summarizer=summarizer,
                hot_window=12,
                clock=lambda: now[0],
            )

            with self.assertLogs("xagent.core.working_context", level="WARNING") as logs:
                failed = await compactor.ensure_fresh()
            during_cooldown = await compactor.ensure_fresh()

            self.assertEqual(failed.summary, "keep me")
            self.assertEqual(during_cooldown.summary, "keep me")
            self.assertEqual(summarizer.calls, 1)
            self.assertIn("retrying after 60s", "\n".join(logs.output))

            now[0] += WORKING_CONTEXT_FAILURE_COOLDOWN_SECONDS
            recovered = await compactor.ensure_fresh()

            self.assertEqual(summarizer.calls, 2)
            self.assertEqual(recovered.summary, "recovered summary")
            self.assertEqual(recovered.covers_through_cursor, 18)
            self.assertEqual(compactor._retry_not_before, 0.0)

    async def test_cursor_failure_is_not_retried_during_cooldown(self):
        class BrokenCursorStorage:
            def __init__(self):
                self.calls = 0

            async def get_latest_message_cursor(self):
                self.calls += 1
                raise RuntimeError("cursor unavailable")

        now = [200.0]
        storage = BrokenCursorStorage()
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WorkingContextStore(Path(tmpdir) / ".working_context.json")
            store.write(WorkingContextState(summary="last good summary"))
            compactor = WorkingContextCompactor(
                store=store,
                message_storage=storage,
                summarizer=_FakeSummarizer(),
                hot_window=12,
                clock=lambda: now[0],
            )

            with self.assertLogs("xagent.core.working_context", level="WARNING"):
                first = await compactor.ensure_fresh()
            second = await compactor.ensure_fresh()

            self.assertEqual(first, second)
            self.assertEqual(first.summary, "last good summary")
            self.assertEqual(storage.calls, 1)

    async def test_empty_summary_does_not_replace_state_or_advance_coverage(self):
        class EmptySummarizer:
            async def summarize(self, **kwargs):
                del kwargs
                return "  "

        messages = [
            Message.create(f"m-{index:02d}", role=RoleType.USER, sender_id="alice")
            for index in range(30)
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WorkingContextStore(Path(tmpdir) / ".working_context.json")
            store.write(WorkingContextState(summary="last good summary"))
            compactor = WorkingContextCompactor(
                store=store,
                message_storage=_FakeMessageStorage(messages),
                summarizer=EmptySummarizer(),
                hot_window=12,
            )

            with self.assertLogs("xagent.core.working_context", level="WARNING"):
                view = await compactor.ensure_fresh()

            persisted = store.read()
            self.assertEqual(view.summary, "last good summary")
            self.assertEqual(view.covers_through_cursor, 0)
            self.assertEqual(persisted.summary, "last good summary")
            self.assertEqual(persisted.covers_through_cursor, 0)

    async def test_cancelled_background_refresh_keeps_persisted_state(self):
        messages = [
            Message.create(f"m-{index:02d}", role=RoleType.USER, sender_id="alice")
            for index in range(30)
        ]

        class BlockingSummarizer:
            def __init__(self):
                self.started = asyncio.Event()

            async def summarize(self, **kwargs):
                del kwargs
                self.started.set()
                await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as tmpdir:
            store = WorkingContextStore(Path(tmpdir) / ".working_context.json")
            store.write(WorkingContextState(summary="stable summary"))
            summarizer = BlockingSummarizer()
            compactor = WorkingContextCompactor(
                store=store,
                message_storage=_FakeMessageStorage(messages),
                summarizer=summarizer,
                hot_window=12,
            )

            snapshot = await compactor.view_for_turn()
            await asyncio.wait_for(summarizer.started.wait(), timeout=1)
            await compactor.cancel_refresh()

            self.assertEqual(snapshot.summary, "stable summary")
            self.assertEqual(store.read().summary, "stable summary")
            self.assertEqual(store.read().covers_through_cursor, 0)

    async def test_background_refresh_exception_is_consumed_and_deferred(self):
        class BrokenLockStore(WorkingContextStore):
            def acquire_lock(self):
                raise RuntimeError("lock unavailable")

        now = [300.0]
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BrokenLockStore(Path(tmpdir) / ".working_context.json")
            store.write(WorkingContextState(summary="safe snapshot"))
            compactor = WorkingContextCompactor(
                store=store,
                message_storage=_FakeMessageStorage(),
                summarizer=_FakeSummarizer(),
                hot_window=12,
                clock=lambda: now[0],
            )

            with self.assertLogs("xagent.core.working_context", level="WARNING") as logs:
                snapshot = await compactor.view_for_turn()
                task = compactor._refresh_task
                self.assertIsNotNone(task)
                await asyncio.gather(task, return_exceptions=True)
                await asyncio.sleep(0)

            self.assertEqual(snapshot.summary, "safe snapshot")
            self.assertIsNone(compactor._refresh_task)
            self.assertEqual(
                compactor._retry_not_before,
                now[0] + WORKING_CONTEXT_FAILURE_COOLDOWN_SECONDS,
            )
            self.assertIn("background refresh", "\n".join(logs.output))


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

    def test_budget_keeps_uncovered_messages_even_beyond_hot_window(self):
        messages = []
        for index in range(1, 16):
            messages.append(
                Message.create(
                    f"m-{index:02d}",
                    role=RoleType.USER,
                    sender_id="alice",
                ).model_copy(
                    update={
                        "metadata": {
                            AgentConfig.MESSAGE_STORAGE_CURSOR_KEY: index,
                        }
                    }
                )
            )
        selected, omitted = MessageHandler._budget_transcript_entries(
            messages,
            max_messages=12,
            covers_through_cursor=0,
        )
        self.assertEqual(omitted, 0)
        self.assertEqual(len(selected), 15)
        self.assertEqual(selected[0][1], "m-01")
        self.assertEqual(selected[-1][1], "m-15")

    def test_budget_drops_only_messages_already_covered_by_summary(self):
        messages = []
        for index in range(1, 21):
            messages.append(
                Message.create(
                    f"m-{index:02d}",
                    role=RoleType.USER,
                    sender_id="alice",
                ).model_copy(
                    update={
                        "metadata": {
                            AgentConfig.MESSAGE_STORAGE_CURSOR_KEY: index,
                        }
                    }
                )
            )
        selected, omitted = MessageHandler._budget_transcript_entries(
            messages,
            max_messages=12,
            covers_through_cursor=8,
        )
        self.assertEqual(omitted, 0)
        self.assertEqual([content for _, content in selected], [f"m-{i:02d}" for i in range(9, 21)])

    def test_summarizer_prompt_is_not_diary(self):
        system_prompt = WorkingContextSummarizer.build_system_prompt()
        self.assertIn("NOT a diary", system_prompt)
        self.assertIn("speaker attribution", system_prompt.lower())
        self.assertNotIn('first-person ("I")', system_prompt)

    def test_summarizer_has_bounded_maintenance_generation_settings(self):
        summarizer = WorkingContextSummarizer(
            client=object(),
            model="deepseek-v4-flash",
            provider_name="deepseek",
            model_api="openai_chat_completions",
            reasoning=ReasoningConfig(enabled=False),
        )

        self.assertEqual(
            summarizer._llm.max_tokens,
            AgentConfig.WORKING_CONTEXT_SUMMARY_MAX_TOKENS,
        )
        self.assertEqual(summarizer._llm.max_tokens, 2048)
        self.assertEqual(summarizer._llm.reasoning, ReasoningConfig(enabled=False))
        self.assertEqual(summarizer.summary_max_chars, 1500)


if __name__ == "__main__":
    unittest.main()
