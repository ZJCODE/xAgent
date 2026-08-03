"""Tests for memory tool factories (write_memory, search_memory)."""

import os
import tempfile
import unittest
from datetime import date

from xagent.components.memory import MarkdownMemory
from xagent.components.message import MessageStorage
from xagent.schemas import Message, RoleType
from xagent.tools.memory_tool import (
    create_write_memory_tool,
    create_search_memory_tool,
)


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
        result = await tool(query=["Alice"])
        self.assertIn("Alice", result["results"])
        self.assertIn(f"[daily {date.today().isoformat()}]", result["results"])
        self.assertNotIn(str(self.memory.root), result["results"])
        self.assertNotIn(".md:", result["results"])

    async def test_search_memory_disabled(self):
        tool = create_search_memory_tool(self.memory, is_enabled=False)
        result = await tool(query=["anything"])
        self.assertFalse(result["enabled"])

    async def test_search_memory_date_range(self):
        today = date.today()
        await self.memory.append_daily("Entry for today", target_date=today)
        tool = create_search_memory_tool(self.memory, is_enabled=True)
        result = await tool(date=today.isoformat())
        self.assertIn("Entry for today", result["results"])
        self.assertIn(f"[daily {today.isoformat()}]", result["results"])
        self.assertNotIn(str(self.memory.root), result["results"])

    async def test_search_memory_multi_terms_or_match(self):
        await self.memory.append_daily("Jun 推荐了几本关于阅读的书，周末一起讨论")
        await self.memory.append_daily("完全无关的天气记录")
        tool = create_search_memory_tool(self.memory, is_enabled=True)
        result = await tool(query=["Jun", "书", "阅读", "推荐"])
        self.assertIn("Jun", result["results"])
        self.assertIn("书", result["results"])
        self.assertNotIn("天气记录", result["results"])
        self.assertNotIn(str(self.memory.root), result["results"])

    async def test_search_memory_multi_terms_partial_hits(self):
        await self.memory.append_daily("Jun 喜欢爬山和摄影")
        tool = create_search_memory_tool(self.memory, is_enabled=True)
        result = await tool(query=["Jun", "喜欢", "爱好", "兴趣"])
        self.assertIn("Jun", result["results"])
        self.assertIn("喜欢", result["results"])

    async def test_search_memory_ranks_higher_cooccurrence_first(self):
        await self.memory.append_daily("书架上落了灰")
        await self.memory.append_daily("Jun 推荐阅读《三体》这本书")
        tool = create_search_memory_tool(self.memory, is_enabled=True)
        result = await tool(query=["Jun", "书", "阅读", "推荐"], context_lines=0)
        jun_pos = result["results"].find("Jun")
        shelf_pos = result["results"].find("书架")
        self.assertGreaterEqual(jun_pos, 0)
        self.assertGreaterEqual(shelf_pos, 0)
        self.assertLess(jun_pos, shelf_pos)

    async def test_search_memory_keyword_finds_sqlite_messages(self):
        """Keyword search returns results from SQLite messages when diary is empty."""
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tmp:
            db_path = tmp.name
        try:
            msg_storage = MessageStorage(path=db_path)
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
            result = await tool(query=["deployment"])

            self.assertIn("deployment", result["results"])
            self.assertIn("Q3 deployment plan", result["results"])
            self.assertIn("speaker=alice", result["results"])
            self.assertTrue(result["enabled"])
        finally:
            os.unlink(db_path)

    async def test_search_memory_keyword_merges_diary_and_sqlite(self):
        """Results from both diary files and SQLite messages are merged."""
        await self.memory.append_daily("Morning standup: decided to refactor auth module")

        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tmp:
            db_path = tmp.name
        try:
            msg_storage = MessageStorage(path=db_path)
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
            result = await tool(query=["refactor"])

            self.assertIn("refactor", result["results"])
            self.assertIn("Morning standup", result["results"])
            self.assertIn("Message Store", result["results"])
            self.assertIn("JWT validation", result["results"])
        finally:
            os.unlink(db_path)

    async def test_search_memory_keyword_sqlite_only_when_no_diary_match(self):
        """SQLite results are still returned even when diary has no match."""
        await self.memory.append_daily("Went shopping for groceries")

        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tmp:
            db_path = tmp.name
        try:
            msg_storage = MessageStorage(path=db_path)
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
            result = await tool(query=["SSL"])

            self.assertIn("SSL certificate", result["results"])
            self.assertIn("speaker=assistant", result["results"])
        finally:
            os.unlink(db_path)

    async def test_search_memory_keyword_with_date_filters_sqlite(self):
        """Date-scoped keyword search also searches SQLite with date filter."""
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tmp:
            db_path = tmp.name
        try:
            msg_storage = MessageStorage(path=db_path)
            today_str = date.today().isoformat()
            msg = Message.create(
                content="Today's task: review the API documentation",
                role=RoleType.USER,
                sender_id="alice",
            )
            await msg_storage.add_messages(msg)

            raw_results = await msg_storage.search_messages(
                terms=["API documentation"],
                date_start=today_str,
            )
            self.assertIn("API documentation", raw_results)

            tool = create_search_memory_tool(
                self.memory,
                is_enabled=True,
                message_storage=msg_storage,
            )
            result = await tool(query=["API documentation"], date=today_str)

            self.assertIn("API documentation", result["results"])
        finally:
            os.unlink(db_path)

    async def test_search_memory_phrase_term_still_works(self):
        """A single list element may be a multi-word phrase needle."""
        await self.memory.append_daily("Legacy entry: old project notes")
        tool = create_search_memory_tool(self.memory, is_enabled=True)
        result = await tool(query=["project notes"])
        self.assertIn("project notes", result["results"])
        self.assertNotIn("Message Store", result["results"])

    async def test_search_messages_caps_result_count(self):
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tmp:
            db_path = tmp.name
        try:
            msg_storage = MessageStorage(path=db_path)
            for index in range(35):
                await msg_storage.add_messages(
                    Message.create(
                        content=f"unique-token hit number {index}",
                        role=RoleType.USER,
                        sender_id="alice",
                    )
                )
            raw = await msg_storage.search_messages(terms=["unique-token"], max_results=20)
            # Each match is one block separated by ---
            blocks = [block for block in raw.split("\n---\n") if block.strip()]
            self.assertLessEqual(len(blocks), 20)
            self.assertGreaterEqual(len(blocks), 1)
        finally:
            os.unlink(db_path)

    async def test_search_messages_caps_char_budget(self):
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tmp:
            db_path = tmp.name
        try:
            msg_storage = MessageStorage(path=db_path)
            long_body = "TOKEN " + ("x" * 3000)
            for _ in range(5):
                await msg_storage.add_messages(
                    Message.create(
                        content=long_body,
                        role=RoleType.USER,
                        sender_id="alice",
                    )
                )
            raw = await msg_storage.search_messages(
                terms=["TOKEN"],
                max_results=20,
                max_chars=6000,
            )
            self.assertLessEqual(len(raw), 6000 + 16)  # small slack for separators
            self.assertIn("TOKEN", raw)
        finally:
            os.unlink(db_path)

    async def test_search_memory_tool_applies_message_count_cap(self):
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tmp:
            db_path = tmp.name
        try:
            msg_storage = MessageStorage(path=db_path)
            for index in range(30):
                await msg_storage.add_messages(
                    Message.create(
                        content=f"budget-cap-{index} marker",
                        role=RoleType.USER,
                        sender_id="bob",
                    )
                )
            tool = create_search_memory_tool(
                self.memory,
                is_enabled=True,
                message_storage=msg_storage,
            )
            result = await tool(query=["budget-cap"])
            blocks = [
                block
                for block in result["results"].split("\n---\n")
                if "budget-cap" in block
            ]
            self.assertLessEqual(len(blocks), 20)
        finally:
            os.unlink(db_path)

    async def test_diary_search_caps_result_count(self):
        for index in range(25):
            await self.memory.append_daily(f"diary-cap marker {index}")
        result = await self.memory.search_keyword(
            ["diary-cap"],
            context_lines=0,
            max_results=20,
            max_chars=100_000,
        )
        blocks = [block for block in result.split("\n---\n") if block.strip()]
        self.assertLessEqual(len(blocks), 20)
        self.assertIn("[daily ", result)


if __name__ == "__main__":
    unittest.main()
