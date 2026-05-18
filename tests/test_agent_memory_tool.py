"""Tests for memory tool factories."""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from xagent.components.memory import SQLiteMemory
from xagent.components.message import MessageStorageLocal
from xagent.schemas import Message, RoleType
from xagent.tools.memory_tool import (
    create_query_memory_tool,
    create_query_messages_tool,
    create_write_memory_tool,
)


class _FakeLLMService:
    async def generate_summary(self, source_content, period_type, period_label):
        return f"[Summary: {period_type} {period_label}]"


class MemoryToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.memory = SQLiteMemory(str(Path(self._tmpdir.name) / "memory.sqlite3"))
        self.messages = MessageStorageLocal(str(Path(self._tmpdir.name) / "messages.sqlite3"))
        self._enabled = True

    def tearDown(self):
        self._tmpdir.cleanup()

    def _is_enabled(self):
        return self._enabled

    async def test_write_memory_records_entry(self):
        tool = create_write_memory_tool(self.memory, self._is_enabled)
        result = await tool("This is a test memory note")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["message"], "Memory recorded.")
        self.assertNotIn("file", result)

        result = await self.memory.query_sql("SELECT content, source FROM memory_entries")
        self.assertIn("test memory note", result["rows"][0]["content"])
        self.assertEqual(result["rows"][0]["source"], "tool")

    async def test_write_memory_disabled(self):
        self._enabled = False
        tool = create_write_memory_tool(self.memory, self._is_enabled)
        result = await tool("Should not be written")
        self.assertEqual(result["status"], "disabled")

    async def test_write_memory_empty_content(self):
        tool = create_write_memory_tool(self.memory, self._is_enabled)
        result = await tool("   ")
        self.assertEqual(result["status"], "skipped")

    async def test_query_memory_sql(self):
        await self.memory.add_entry("Meeting with Alice about project X", target_date=date.today())
        tool = create_query_memory_tool(self.memory, self._is_enabled)
        result = await tool(sql="SELECT content FROM memory_entries WHERE content LIKE '%Alice%'")
        self.assertEqual(result["status"], "ok")
        self.assertIn("Alice", result["rows"][0]["content"])

    async def test_query_memory_disabled(self):
        self._enabled = False
        tool = create_query_memory_tool(self.memory, self._is_enabled)
        result = await tool(sql="SELECT * FROM memory_entries")
        self.assertFalse(result["enabled"])

    async def test_query_memory_rejects_write_sql(self):
        tool = create_query_memory_tool(self.memory, self._is_enabled)
        result = await tool(sql="DELETE FROM memory_entries")
        self.assertEqual(result["status"], "error")
        self.assertIn("Only SELECT or WITH", result["message"])

    async def test_query_messages_sql(self):
        await self.messages.add_messages(
            Message.create("Older project detail", role=RoleType.USER, sender_id="Alice")
        )
        tool = create_query_messages_tool(self.messages, self._is_enabled)
        result = await tool(sql="SELECT sender_id, content FROM messages WHERE content LIKE '%project%'")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["rows"][0]["sender_id"], "Alice")

    async def test_query_messages_rejects_multi_statement(self):
        tool = create_query_messages_tool(self.messages, self._is_enabled)
        result = await tool(sql="SELECT * FROM messages; SELECT * FROM messages")
        self.assertEqual(result["status"], "error")
        self.assertIn("Only one SQL statement", result["message"])


if __name__ == "__main__":
    unittest.main()
