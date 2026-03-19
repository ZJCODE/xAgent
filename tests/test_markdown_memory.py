"""Tests for MarkdownMemory (file-based diary storage)."""

import asyncio
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from xagent.components.memory.markdown_memory import MarkdownMemory


class MarkdownMemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.memory_dir = self._tmpdir.name
        self.memory = MarkdownMemory(self.memory_dir)

    def tearDown(self):
        self._tmpdir.cleanup()

    async def test_append_daily_creates_file(self):
        today = date.today()
        path = await self.memory.append_daily("Hello world")
        self.assertTrue(path.exists())
        text = path.read_text()
        self.assertIn("Hello world", text)

    async def test_append_daily_appends_multiple_entries(self):
        today = date.today()
        await self.memory.append_daily("First entry")
        await self.memory.append_daily("Second entry")
        text = await self.memory.read_file(self.memory.daily_path(today))
        self.assertIn("First entry", text)
        self.assertIn("Second entry", text)

    async def test_daily_path_format(self):
        d = date(2025, 3, 15)
        path = self.memory.daily_path(d)
        self.assertEqual(path.name, "2025-03-15.md")
        self.assertIn("2025-03", str(path))

    async def test_read_recent_dailies_returns_entries(self):
        today = date.today()
        yesterday = today - timedelta(days=1)
        await self.memory.append_daily("Today notes", target_date=today)
        await self.memory.append_daily("Yesterday notes", target_date=yesterday)

        results = await self.memory.read_recent_dailies(days=2)
        dates = [r[0] for r in results]
        self.assertIn(today.isoformat(), dates)
        self.assertIn(yesterday.isoformat(), dates)

    async def test_read_recent_dailies_empty_for_no_data(self):
        results = await self.memory.read_recent_dailies(days=3)
        self.assertEqual(results, [])

    async def test_write_and_read_summary(self):
        d = date.today()
        start, end = self.memory.week_range_for(d)
        wp = self.memory.weekly_path(start, end)

        await self.memory.write_summary(wp, "Weekly summary content")
        text = await self.memory.read_file(wp)
        self.assertIn("Weekly summary content", text)

    async def test_search_keyword(self):
        await self.memory.append_daily("Important meeting with Alice")
        await self.memory.append_daily("Lunch with Bob")

        result = await self.memory.search_keyword("Alice")
        self.assertIn("Alice", result)

    async def test_search_keyword_no_matches(self):
        await self.memory.append_daily("Something unrelated")
        result = await self.memory.search_keyword("nonexistent_xyz_term")
        self.assertEqual(result.strip(), "")

    async def test_search_date_range(self):
        today = date.today()
        yesterday = today - timedelta(days=1)
        await self.memory.append_daily("Today stuff", target_date=today)
        await self.memory.append_daily("Yesterday stuff", target_date=yesterday)

        result = await self.memory.search_date_range(
            start=yesterday.isoformat(),
            end=today.isoformat(),
        )
        self.assertIn("Today stuff", result)
        self.assertIn("Yesterday stuff", result)

    async def test_list_files(self):
        await self.memory.append_daily("entry 1")
        files = await self.memory.list_files("daily")
        self.assertTrue(len(files) >= 1)
        self.assertTrue(all(f.endswith(".md") for f in files))

    async def test_week_range_for(self):
        d = date(2025, 7, 9)  # Wednesday
        monday, sunday = MarkdownMemory.week_range_for(d)
        self.assertEqual(monday.weekday(), 0)  # Monday
        self.assertEqual(sunday.weekday(), 6)  # Sunday
        self.assertLessEqual(monday, d)
        self.assertGreaterEqual(sunday, d)

    async def test_directory_structure_created(self):
        root = Path(self.memory_dir)
        for sub in ("daily", "weekly", "monthly", "yearly"):
            self.assertTrue((root / sub).is_dir())


if __name__ == "__main__":
    unittest.main()
