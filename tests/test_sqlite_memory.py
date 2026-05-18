"""Tests for SQLiteMemory long-term storage."""

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from xagent.components.memory import SQLiteMemory


class SQLiteMemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "memory.sqlite3"
        self.memory = SQLiteMemory(str(self.db_path))

    def tearDown(self):
        self._tmpdir.cleanup()

    async def test_add_entry_creates_database_row(self):
        row_id = await self.memory.add_entry("Hello world", source="tool")

        self.assertGreater(row_id, 0)
        result = await self.memory.query_sql("SELECT source, content FROM memory_entries")
        self.assertEqual(result["rows"][0]["source"], "tool")
        self.assertIn("Hello world", result["rows"][0]["content"])

    async def test_read_recent_entries_groups_by_date(self):
        today = date.today()
        yesterday = today - timedelta(days=1)
        await self.memory.add_entry("Today notes", target_date=today)
        await self.memory.add_entry("Yesterday notes", target_date=yesterday)

        results = await self.memory.read_recent_entries(days=2)
        dates = [item[0] for item in results]

        self.assertIn(today.isoformat(), dates)
        self.assertIn(yesterday.isoformat(), dates)

    async def test_search_date_range_formats_entries(self):
        today = date.today()
        yesterday = today - timedelta(days=1)
        await self.memory.add_entry("Today stuff", target_date=today)
        await self.memory.add_entry("Yesterday stuff", target_date=yesterday)

        result = await self.memory.search_date_range(
            start=yesterday.isoformat(),
            end=today.isoformat(),
        )

        self.assertIn("Today stuff", result)
        self.assertIn("Yesterday stuff", result)

    async def test_upsert_summary_and_exists(self):
        period_start = date(2026, 5, 11)
        period_end = date(2026, 5, 17)

        self.assertFalse(
            await self.memory.summary_exists(
                period_type="weekly",
                period_start=period_start,
                period_end=period_end,
            )
        )

        await self.memory.upsert_summary(
            period_type="weekly",
            period_start=period_start,
            period_end=period_end,
            content="Weekly summary content",
        )

        self.assertTrue(
            await self.memory.summary_exists(
                period_type="weekly",
                period_start=period_start,
                period_end=period_end,
            )
        )
        result = await self.memory.query_sql("SELECT content FROM memory_summaries")
        self.assertEqual(result["rows"][0]["content"], "Weekly summary content")

    async def test_people_facts_dedupe(self):
        fact = {
            "fact": "Alice prefers concise implementation plans.",
            "evidence": "I prefer concise implementation plans.",
            "source": "direct message",
        }

        inserted_one = await self.memory.add_people_facts("Alice", [fact], display_name="Alice")
        inserted_two = await self.memory.add_people_facts("Alice", [fact], display_name="Alice")

        self.assertEqual(inserted_one, 1)
        self.assertEqual(inserted_two, 0)
        result = await self.memory.query_sql("SELECT person_key, fact, evidence FROM people_facts")
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["person_key"], "Alice")

    async def test_readonly_query_rejects_writes(self):
        result = None
        with self.assertRaisesRegex(ValueError, "Only SELECT or WITH"):
            await self.memory.query_sql("INSERT INTO memory_entries(content) VALUES ('x')")


if __name__ == "__main__":
    unittest.main()
