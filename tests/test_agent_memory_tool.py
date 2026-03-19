"""Tests for memory tool factories (write_daily_memory, search_memory, generate_memory_summary)."""

import asyncio
import tempfile
import unittest
from datetime import date

from xagent.components.memory.markdown_memory import MarkdownMemory
from xagent.tools.memory_tool import (
    create_write_daily_memory_tool,
    create_search_memory_tool,
    create_generate_summary_tool,
)


class _FakeLLMService:
    async def generate_summary(self, source_content, period_type, period_label):
        return f"[Summary: {period_type} {period_label}]"


class MemoryToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.memory = MarkdownMemory(self._tmpdir.name)
        self._enabled = True

    def tearDown(self):
        self._tmpdir.cleanup()

    def _is_enabled(self):
        return self._enabled

    async def test_write_daily_memory_appends_entry(self):
        tool = create_write_daily_memory_tool(self.memory, self._is_enabled)
        result = await tool("This is a test diary entry")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["date"], date.today().isoformat())

        text = await self.memory.read_file(self.memory.daily_path(date.today()))
        self.assertIn("test diary entry", text)

    async def test_write_daily_memory_disabled(self):
        self._enabled = False
        tool = create_write_daily_memory_tool(self.memory, self._is_enabled)
        result = await tool("Should not be written")
        self.assertEqual(result["status"], "disabled")

    async def test_write_daily_memory_empty_content(self):
        tool = create_write_daily_memory_tool(self.memory, self._is_enabled)
        result = await tool("   ")
        self.assertEqual(result["status"], "skipped")

    async def test_search_memory_keyword(self):
        await self.memory.append_daily("Meeting with Alice about project X")
        tool = create_search_memory_tool(self.memory, self._is_enabled)
        result = await tool(query="Alice")
        self.assertIn("Alice", result["results"])

    async def test_search_memory_disabled(self):
        self._enabled = False
        tool = create_search_memory_tool(self.memory, self._is_enabled)
        result = await tool(query="anything")
        self.assertFalse(result["enabled"])

    async def test_search_memory_date_range(self):
        today = date.today()
        await self.memory.append_daily("Entry for today", target_date=today)
        tool = create_search_memory_tool(self.memory, self._is_enabled)
        result = await tool(date=today.isoformat())
        self.assertIn("Entry for today", result["results"])


if __name__ == "__main__":
    unittest.main()
