"""Tests for MarkdownMemory (file-based diary storage)."""

import asyncio
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from xagent.components.memory import MarkdownMemory


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

    async def test_append_daily_uses_date_time_heading_and_plain_body(self):
        target = date(2026, 6, 28)
        with patch("xagent.components.memory.markdown_memory.datetime") as mock_datetime:
            mock_datetime.now.return_value = datetime(2026, 6, 28, 9, 38)
            await self.memory.append_daily("Plain diary body", target_date=target)

        text = await self.memory.read_file(self.memory.daily_path(target))

        self.assertIn("## 2026-06-28 09:38", text)
        self.assertIn("\n\nPlain diary body\n", text)
        self.assertNotIn("## 09:38", text)
        self.assertNotRegex(text, r"(?m)^---\s*$")

    async def test_append_daily_appends_multiple_entries(self):
        today = date.today()
        await self.memory.append_daily("First entry")
        await self.memory.append_daily("Second entry")
        text = await self.memory.read_file(self.memory.daily_path(today))
        self.assertIn("First entry", text)
        self.assertIn("Second entry", text)
        self.assertNotRegex(text, r"(?m)^---\s*$")

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

        result = await self.memory.search_keyword(["Alice"])
        self.assertIn("Alice", result)
        today = date.today().isoformat()
        self.assertIn(f"[daily {today}]", result)
        self.assertNotIn(str(self.memory.root), result)
        self.assertNotIn(".md:", result)

    async def test_search_keyword_notes_scope(self):
        notes_dir = Path(self.memory_dir) / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)
        (notes_dir / "home-wifi.md").write_text(
            '<!-- note slug="home-wifi" title="Home wifi" updated="2026-08-19" -->\n\n'
            "SSID orchard lives on the router.\n",
            encoding="utf-8",
        )
        result = await self.memory.search_keyword(["orchard"], scope="notes")
        self.assertIn("orchard", result)
        self.assertIn("[note home-wifi]", result)
        self.assertIn("SSID orchard lives on the router.", result)
        self.assertIn('slug="home-wifi"', result)
        self.assertIn("links: none | backlinks: none", result)
        self.assertNotIn(str(self.memory.root), result)

        listed = await self.memory.list_files(scope="notes")
        self.assertIn("[note home-wifi]", listed)

    async def test_search_keyword_no_matches(self):
        await self.memory.append_daily("Something unrelated")
        result = await self.memory.search_keyword(["nonexistent_xyz_term"])
        self.assertEqual(result.strip(), "")

    async def test_search_keyword_multi_terms(self):
        await self.memory.append_daily("Jun 推荐阅读几本书")
        result = await self.memory.search_keyword(["Jun", "书", "阅读", "推荐"])
        self.assertIn("Jun", result)
        self.assertIn("书", result)
        self.assertIn(f"[daily {date.today().isoformat()}]", result)
        self.assertNotIn(str(self.memory.root), result)

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
        self.assertIn(f"[daily {today.isoformat()}]", result)
        self.assertIn(f"[daily {yesterday.isoformat()}]", result)
        self.assertNotIn(str(self.memory.root), result)

    async def test_list_files(self):
        await self.memory.append_daily("entry 1")
        files = await self.memory.list_files("daily")
        self.assertTrue(len(files) >= 1)
        self.assertTrue(all(f.startswith("[daily ") and f.endswith("]") for f in files))
        self.assertTrue(all(str(self.memory.root) not in f for f in files))
        self.assertTrue(all(".md" not in f for f in files))

    async def test_label_for_path_variants(self):
        daily = self.memory.daily_path(date(2026, 8, 3))
        self.assertEqual(self.memory._label_for_path(daily), "[daily 2026-08-03]")

        weekly = self.memory.weekly_path(date(2026, 7, 28), date(2026, 8, 3))
        self.assertEqual(
            self.memory._label_for_path(weekly),
            "[weekly 2026-07-28 to 2026-08-03]",
        )

        monthly = self.memory.monthly_path(2026, 8)
        self.assertEqual(self.memory._label_for_path(monthly), "[monthly 2026-08]")

        yearly = self.memory.yearly_path(2026)
        self.assertEqual(self.memory._label_for_path(yearly), "[yearly 2026]")

    async def test_search_same_score_prefers_newer_daily(self):
        older = date(2026, 1, 1)
        newer = date(2026, 8, 1)
        await self.memory.append_daily("shared-token note", target_date=older)
        await self.memory.append_daily("shared-token note", target_date=newer)
        result = await self.memory.search_keyword(
            ["shared-token"],
            context_lines=0,
            max_results=2,
            max_chars=100_000,
        )
        newer_pos = result.find("[daily 2026-08-01]")
        older_pos = result.find("[daily 2026-01-01]")
        self.assertGreaterEqual(newer_pos, 0)
        self.assertGreaterEqual(older_pos, 0)
        self.assertLess(newer_pos, older_pos)

    async def test_search_higher_score_beats_newer_lower_score(self):
        older = date(2026, 1, 1)
        newer = date(2026, 8, 1)
        await self.memory.append_daily("Alpha Beta Gamma all three", target_date=older)
        await self.memory.append_daily("Alpha only", target_date=newer)
        result = await self.memory.search_keyword(
            ["Alpha", "Beta", "Gamma"],
            context_lines=0,
            max_results=2,
            max_chars=100_000,
        )
        old_pos = result.find("Alpha Beta Gamma")
        new_pos = result.find("Alpha only")
        self.assertGreaterEqual(old_pos, 0)
        self.assertGreaterEqual(new_pos, 0)
        self.assertLess(old_pos, new_pos)

    async def test_event_time_for_path_variants(self):
        daily = self.memory.daily_path(date(2026, 8, 3))
        weekly = self.memory.weekly_path(date(2026, 7, 28), date(2026, 8, 3))
        monthly = self.memory.monthly_path(2026, 8)
        yearly = self.memory.yearly_path(2026)
        self.assertGreater(
            self.memory._event_time_for_path(daily),
            0.0,
        )
        self.assertEqual(
            self.memory._event_time_for_path(daily),
            self.memory._event_time_for_path(weekly),
        )
        self.assertGreater(
            self.memory._event_time_for_path(monthly),
            self.memory._event_time_for_path(daily),
        )
        self.assertGreater(
            self.memory._event_time_for_path(yearly),
            self.memory._event_time_for_path(monthly),
        )

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

    async def test_regular_file_io_does_not_spawn_subprocesses(self):
        d = date.today()
        start, end = self.memory.week_range_for(d)
        summary_path = self.memory.weekly_path(start, end)

        with patch("asyncio.create_subprocess_exec", side_effect=AssertionError("subprocess used")):
            await self.memory.append_daily("Native append", target_date=d)
            text = await self.memory.read_file(self.memory.daily_path(d))
            await self.memory.write_summary(summary_path, "Native summary")
            files = await self.memory.list_files("all")

        self.assertIn("Native append", text)
        self.assertIn(f"[weekly {start.isoformat()} to {end.isoformat()}]", files)
        self.assertIn(f"[daily {d.isoformat()}]", files)


if __name__ == "__main__":
    unittest.main()
