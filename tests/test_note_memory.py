"""Tests for topic-indexed standing notes (store, write tool, search)."""

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from xagent.components.memory import MarkdownMemory, NoteStore, NoteStoreError, slugify
from xagent.components.memory.note_memory import NOTE_BODY_MAX_CHARS, extract_wiki_links
from xagent.core.config import AgentConfig
from xagent.tools.memory_tool import (
    create_search_memory_tool,
    create_upsert_note_tool,
    create_write_memory_tool,
)


class NoteStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = NoteStore(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_slugify_keeps_unicode_and_sanitizes(self):
        self.assertEqual(slugify("家里的网络"), "家里的网络")
        self.assertEqual(slugify("Home Wi-Fi"), "home-wi-fi")
        self.assertEqual(slugify("  Release Process!!  "), "release-process")
        self.assertEqual(slugify(""), "note")

    def test_extract_wiki_links_dedupes_and_slugifies(self):
        body = "See [[home-wifi]] then [[Release Process|ship]] and [[home-wifi]] again."
        self.assertEqual(extract_wiki_links(body), ["home-wifi", "release-process"])
        self.assertEqual(extract_wiki_links("[[ 家里的网络 ]]"), ["家里的网络"])
        self.assertEqual(extract_wiki_links("no links here"), [])
        self.assertEqual(extract_wiki_links("[[|label only]]"), [])

    async def test_upsert_then_read_roundtrip(self):
        page = await self.store.upsert_page(
            title="家里的网络",
            body="家里的 Wi-Fi 是 orchard，密码写在路由器背面。",
        )
        self.assertEqual(page.slug, "家里的网络")
        loaded = await self.store.read_page(page.slug)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.title, "家里的网络")
        self.assertIn("orchard", loaded.body)
        rendered = (self.store.root / "家里的网络.md").read_text(encoding="utf-8")
        self.assertIn('slug="家里的网络"', rendered)
        self.assertIn('title="家里的网络"', rendered)

    async def test_upsert_overwrites_in_place(self):
        await self.store.upsert_page(title="Release", body="Old process.")
        updated = await self.store.upsert_page(
            title="Release",
            body="New Friday ship checklist.",
            slug="release",
        )
        self.assertEqual(updated.slug, "release")
        loaded = await self.store.read_page("release")
        self.assertIn("Friday", loaded.body)
        self.assertNotIn("Old process", loaded.body)
        self.assertEqual(await self.store.count_pages(), 1)

    async def test_resolve_by_title(self):
        await self.store.upsert_page(title="Ship checklist", body="Tag, then deploy.")
        loaded = await self.store.resolve_page("Ship checklist")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.slug, "ship-checklist")

    async def test_body_too_long_is_rejected(self):
        with self.assertRaises(NoteStoreError) as raised:
            await self.store.upsert_page(
                title="Too long",
                body="x" * (NOTE_BODY_MAX_CHARS + 1),
            )
        self.assertEqual(raised.exception.code, "body_too_long")

    async def test_notebook_full_requires_archive(self):
        with patch("xagent.components.memory.note_memory.NOTE_MAX_PAGES", 2):
            store = NoteStore(self._tmpdir.name)
            await store.upsert_page(title="A", body="alpha")
            await store.upsert_page(title="B", body="beta")
            with self.assertRaises(NoteStoreError) as raised:
                await store.upsert_page(title="C", body="gamma")
            self.assertEqual(raised.exception.code, "notebook_full")
            await store.archive_page("a")
            created = await store.upsert_page(title="C", body="gamma")
            self.assertEqual(created.slug, "c")
            self.assertEqual(await store.count_pages(), 2)

    async def test_archive_moves_page_out_of_active_list(self):
        await self.store.upsert_page(title="Stale", body="outdated standing fact")
        archived = await self.store.archive_page("stale")
        self.assertEqual(archived.slug, "stale")
        self.assertIsNone(await self.store.read_page("stale"))
        self.assertEqual(await self.store.list_pages(), [])
        archive_files = list((self.store.root / "archive").glob("*.md"))
        self.assertEqual(len(archive_files), 1)
        self.assertIn("outdated standing fact", archive_files[0].read_text(encoding="utf-8"))

    async def test_backlinks_are_derived_from_other_pages(self):
        await self.store.upsert_page(title="Fire", body="Cooking starts with heat.")
        await self.store.upsert_page(
            title="Weeknight cooking",
            body="Keep it simple. Heat lives on [[fire]].",
        )
        hits = await self.store.backlinks("fire")
        self.assertEqual([page.slug for page in hits], ["weeknight-cooking"])
        self.assertEqual(await self.store.backlinks("weeknight-cooking"), [])
        fire = await self.store.read_page("fire")
        self.assertEqual(fire.links, [])
        cooking = await self.store.read_page("weeknight-cooking")
        self.assertEqual(cooking.links, ["fire"])


class NoteToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = NoteStore(str(Path(self._tmpdir.name) / "notes"))
        self.memory = MarkdownMemory(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    async def test_upsert_note_tool_writes_page(self):
        upsert = create_upsert_note_tool(self.store)
        saved = await upsert(title="Home wifi", content="SSID orchard.")
        self.assertEqual(saved["status"], "ok")
        self.assertEqual(saved["slug"], "home-wifi")
        self.assertEqual(saved["links"], [])
        self.assertEqual(saved["backlinks"], [])
        self.assertNotIn("warning", saved)
        loaded = await self.store.read_page("home-wifi")
        self.assertIn("orchard", loaded.body)

    async def test_upsert_note_tool_returns_links_and_orphan_warning(self):
        upsert = create_upsert_note_tool(self.store)
        first = await upsert(title="Fire", content="Cooking starts with heat.")
        self.assertNotIn("warning", first)
        orphan = await upsert(title="Unattached", content="A second idea with no links.")
        self.assertEqual(orphan["status"], "ok")
        self.assertEqual(orphan["warning"], "orphan")
        self.assertEqual(orphan["links"], [])
        linked = await upsert(
            title="Weeknight cooking",
            content="Keep it simple. Heat lives on [[fire]].",
        )
        self.assertNotIn("warning", linked)
        self.assertEqual(linked["links"], ["fire"])
        self.assertEqual(linked["backlinks"], [])
        fire = await upsert(
            title="Fire",
            content="Cooking starts with heat. I use this in [[weeknight-cooking]].",
            slug="fire",
        )
        self.assertEqual(fire["links"], ["weeknight-cooking"])
        self.assertEqual(fire["backlinks"], ["weeknight-cooking"])

    async def test_write_memory_stays_on_diary(self):
        tool = create_write_memory_tool(self.memory)
        result = await tool("Today we decided the ship window.")
        self.assertEqual(result["status"], "ok")
        daily = await self.memory.read_file(self.memory.daily_path(date.today()))
        self.assertIn("ship window", daily)
        self.assertEqual(await self.store.count_pages(), 0)

    async def test_search_memory_scope_notes_returns_whole_page(self):
        await self.store.upsert_page(
            title="Release process",
            body="Friday tag then deploy.\nKeep the changelog in the repo.",
        )
        tool = create_search_memory_tool(self.memory)
        result = await tool(query=["Friday"], scope="notes")
        self.assertIn("Friday", result["results"])
        self.assertIn("Keep the changelog in the repo.", result["results"])
        self.assertIn("[note release-process]", result["results"])
        self.assertIn("links: none | backlinks: none", result["results"])
        self.assertNotIn(str(self.memory.root), result["results"])

    async def test_search_memory_note_hit_includes_derived_links(self):
        await self.store.upsert_page(title="Fire", body="Cooking starts with heat.")
        await self.store.upsert_page(
            title="Weeknight cooking",
            body="Keep it simple. Heat lives on [[fire]].",
        )
        tool = create_search_memory_tool(self.memory)
        result = await tool(query=["Heat lives"], scope="notes")
        self.assertIn("links: [[fire]] | backlinks: none", result["results"])
        fire = await tool(query=["Cooking starts"], scope="notes")
        self.assertIn("links: none | backlinks: [[weeknight-cooking]]", fire["results"])

    async def test_search_memory_all_includes_notes(self):
        await self.memory.append_daily("Went to lunch")
        await self.store.upsert_page(title="Wifi", body="SSID orchard network.")
        tool = create_search_memory_tool(self.memory)
        result = await tool(query=["orchard"], scope="all")
        self.assertIn("orchard", result["results"])
        self.assertIn("[note wifi]", result["results"])


class NotebookPromptRuleTests(unittest.TestCase):
    def test_self_rules_mention_notebook(self):
        prompt = AgentConfig.BASE_AGENT_PROMPT
        self.assertIn("Standing ideas live in your notebook", prompt)
        self.assertIn("One idea per note, in your own words", prompt)
        self.assertIn("[[slug]]", prompt)
        self.assertIn("look them up with search_memory", prompt)


class AgentNotebookWiringTests(unittest.TestCase):
    def test_agent_wires_note_store_and_upsert_only(self):
        from xagent.core.agent import Agent

        with tempfile.TemporaryDirectory() as tmpdir:
            agent = Agent(client=object(), workspace=tmpdir)
            self.assertTrue((Path(tmpdir) / "memory" / "notes").is_dir())
            self.assertIn("upsert_note", agent.tools)
            self.assertIn("search_memory", agent.tools)
            self.assertIn("write_memory", agent.tools)
            self.assertNotIn("read_note", agent.tools)
            self.assertNotIn("list_notes", agent.tools)
            self.assertNotIn("archive_note", agent.tools)
            self.assertEqual(agent.note_store.root, Path(tmpdir) / "memory" / "notes")


if __name__ == "__main__":
    unittest.main()
