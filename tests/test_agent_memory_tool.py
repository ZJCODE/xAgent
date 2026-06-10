"""Tests for memory tool factories (write_memory, search_memory)."""

import asyncio
import tempfile
import unittest
from datetime import date

from xagent.components.memory import MarkdownMemory
from xagent.tools.memory_tool import (
    create_write_memory_tool,
    create_search_memory_tool,
)


class _FakeLLMService:
    async def generate_summary(self, source_content, period_type, period_label):
        return f"[Summary: {period_type} {period_label}]"


class MemoryToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.memory = MarkdownMemory(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    async def test_write_memory_records_entry(self):
        tool = create_write_memory_tool(self.memory, is_enabled=True)
        result = await tool("This is a test memory note")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["message"], "Memory recorded.")
        self.assertNotIn("file", result)

        text = await self.memory.read_file(self.memory.daily_path(date.today()))
        self.assertIn("test memory note", text)

    async def test_write_memory_disabled(self):
        tool = create_write_memory_tool(self.memory, is_enabled=False)
        result = await tool("Should not be written")
        self.assertEqual(result["status"], "disabled")

    async def test_write_memory_empty_content(self):
        tool = create_write_memory_tool(self.memory, is_enabled=True)
        result = await tool("   ")
        self.assertEqual(result["status"], "skipped")

    async def test_search_memory_keyword(self):
        await self.memory.append_daily("Meeting with Alice about project X")
        tool = create_search_memory_tool(self.memory, is_enabled=True)
        result = await tool(query="Alice")
        self.assertIn("Alice", result["results"])

    async def test_search_memory_disabled(self):
        tool = create_search_memory_tool(self.memory, is_enabled=False)
        result = await tool(query="anything")
        self.assertFalse(result["enabled"])

    async def test_search_memory_date_range(self):
        today = date.today()
        await self.memory.append_daily("Entry for today", target_date=today)
        tool = create_search_memory_tool(self.memory, is_enabled=True)
        result = await tool(date=today.isoformat())
        self.assertIn("Entry for today", result["results"])


if __name__ == "__main__":
    unittest.main()
