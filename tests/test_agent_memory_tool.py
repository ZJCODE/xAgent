"""Tests for memory tool factories (write_memory, search_memory)."""

import asyncio
import tempfile
import unittest
from datetime import date

from xagent.infrastructure.storage import MarkdownMemoryStore
from xagent.infrastructure.storage import SQLiteMessageStore
from xagent.domain import Message, RoleType
from xagent.tools.builtins.memory import (
    create_write_memory_tool,
    create_search_memory_tool,
)


class _FakeLLMService:
    async def generate_summary(self, source_content, period_type, period_label):
        return f"[Summary: {period_type} {period_label}]"


class MemoryToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.memory = MarkdownMemoryStore(self._tmpdir.name)

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

    async def test_search_memory_keyword_finds_sqlite_messages(self):
        """Keyword search returns results from SQLite messages when diary is empty."""
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tmp:
            db_path = tmp.name
        try:
            msg_storage = SQLiteMessageStore(path=db_path)
            msg = Message.create(
                content="We discussed the Q3 deployment plan for the new cluster",
                role=RoleType.USER,
                sender_id="alice",
            )
            await msg_storage.add_messages(msg)

            tool = create_search_memory_tool(
                self.memory,
                is_enabled=True,
                message_storage=msg_storage,
            )
            result = await tool(query="deployment")

            self.assertIn("deployment", result["results"])
            self.assertIn("Q3 deployment plan", result["results"])
            self.assertIn("speaker=alice", result["results"])
            self.assertTrue(result["enabled"])
        finally:
            import os
            os.unlink(db_path)

    async def test_search_memory_keyword_merges_diary_and_sqlite(self):
        """Results from both diary files and SQLite messages are merged."""
        await self.memory.append_daily("Morning standup: decided to refactor auth module")

        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tmp:
            db_path = tmp.name
        try:
            msg_storage = SQLiteMessageStore(path=db_path)
            msg = Message.create(
                content="Afternoon: continued refactoring auth, hit a snag with JWT validation",
                role=RoleType.USER,
                sender_id="bob",
            )
            await msg_storage.add_messages(msg)

            tool = create_search_memory_tool(
                self.memory,
                is_enabled=True,
                message_storage=msg_storage,
            )
            result = await tool(query="refactor")

            self.assertIn("refactor", result["results"])
            self.assertIn("Morning standup", result["results"])
            self.assertIn("Message Store", result["results"])
            self.assertIn("JWT validation", result["results"])
        finally:
            import os
            os.unlink(db_path)

    async def test_search_memory_keyword_sqlite_only_when_no_diary_match(self):
        """SQLite results are still returned even when diary has no match."""
        await self.memory.append_daily("Went shopping for groceries")

        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tmp:
            db_path = tmp.name
        try:
            msg_storage = SQLiteMessageStore(path=db_path)
            msg = Message.create(
                content="Remember to update the production SSL certificate before June 30",
                role=RoleType.ASSISTANT,
                sender_id=None,
            )
            await msg_storage.add_messages(msg)

            tool = create_search_memory_tool(
                self.memory,
                is_enabled=True,
                message_storage=msg_storage,
            )
            # "SSL" only appears in SQLite, not diary
            result = await tool(query="SSL")

            self.assertIn("SSL certificate", result["results"])
            # Diary has no SSL match, so SQLite result is the entire output (no separator)
            self.assertIn("speaker=assistant", result["results"])
        finally:
            import os
            os.unlink(db_path)

    async def test_search_memory_keyword_with_date_filters_sqlite(self):
        """Date-scoped keyword search also searches SQLite with date filter."""
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tmp:
            db_path = tmp.name
        try:
            msg_storage = SQLiteMessageStore(path=db_path)
            today_str = date.today().isoformat()
            msg = Message.create(
                content="Today's task: review the API documentation",
                role=RoleType.USER,
                sender_id="alice",
            )
            await msg_storage.add_messages(msg)

            # Verify message storage search works directly
            raw_results = await msg_storage.search_messages(
                query="API documentation",
                date_start=today_str,
            )
            self.assertIn("API documentation", raw_results)

            tool = create_search_memory_tool(
                self.memory,
                is_enabled=True,
                message_storage=msg_storage,
            )
            result = await tool(query="API documentation", date=today_str)

            self.assertIn("API documentation", result["results"])
        finally:
            import os
            os.unlink(db_path)

    async def test_search_memory_no_message_storage_still_works(self):
        """Without message_storage, search works as before (backward compatible)."""
        await self.memory.append_daily("Legacy entry: old project notes")
        tool = create_search_memory_tool(self.memory, is_enabled=True)
        result = await tool(query="project notes")
        self.assertIn("project notes", result["results"])
        self.assertNotIn("Message Store", result["results"])


if __name__ == "__main__":
    unittest.main()
