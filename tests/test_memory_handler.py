"""Tests for MemoryHandler (diary context + background write scheduling)."""

import asyncio
import tempfile
import time
import unittest
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

from xagent.components.memory.markdown_memory import MarkdownMemory
from xagent.core.handlers.memory import MemoryHandler


class _FakeLLMService:
    """Stub that returns the messages joined as a simple entry."""

    async def format_diary_entry(self, messages, journal_date):
        parts = [str(m.get("content", "")) for m in messages if m.get("content")]
        return "\n".join(parts)

    async def generate_summary(self, source_content, period_type, period_label):
        return f"[Summary: {period_type} {period_label}]"


class MemoryHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.memory = MarkdownMemory(self._tmpdir.name)
        self.llm = _FakeLLMService()
        self.handler = MemoryHandler(memory=self.memory, llm_service=self.llm)

    def tearDown(self):
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
        msgs = [{"role": "user", "content": f"msg {i}"} for i in range(self.handler.MESSAGE_THRESHOLD)]
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


if __name__ == "__main__":
    unittest.main()
