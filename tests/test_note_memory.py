"""Tests for topic-indexed standing notes (store, tools, injection)."""

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from xagent.components.memory import MarkdownMemory, NoteStore, NoteStoreError, slugify
from xagent.components.memory.note_memory import NOTE_BODY_MAX_CHARS
from xagent.core.config import AgentConfig
from xagent.core.handlers.memory import MemoryHandler
from xagent.core.handlers.message import MessageHandler
from xagent.tools.memory_tool import (
    create_archive_note_tool,
    create_list_notes_tool,
    create_read_note_tool,
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
        self.assertFalse(loaded.pinned)
        rendered = (self.store.root / "家里的网络.md").read_text(encoding="utf-8")
        self.assertIn('slug="家里的网络"', rendered)
        self.assertIn('title="家里的网络"', rendered)
        self.assertNotIn("pinned=", rendered)

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

    async def test_pin_limit_rejects_a_third_pin(self):
        await self.store.upsert_page(title="One", body="first", pinned=True)
        await self.store.upsert_page(title="Two", body="second", pinned=True)
        with self.assertRaises(NoteStoreError) as raised:
            await self.store.upsert_page(title="Three", body="third", pinned=True)
        self.assertEqual(raised.exception.code, "pin_limit")
        self.assertEqual(await self.store.count_pages(), 2)

    async def test_repinning_existing_page_does_not_count_as_third(self):
        await self.store.upsert_page(title="One", body="first", pinned=True)
        await self.store.upsert_page(title="Two", body="second", pinned=True)
        updated = await self.store.upsert_page(
            title="One",
            body="first, still current",
            pinned=True,
        )
        self.assertTrue(updated.pinned)

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

    async def test_archive_moves_page_out_of_catalog(self):
        await self.store.upsert_page(title="Stale", body="outdated standing fact")
        archived = await self.store.archive_page("stale")
        self.assertEqual(archived.slug, "stale")
        self.assertIsNone(await self.store.read_page("stale"))
        self.assertEqual(await self.store.list_pages(), [])
        archive_files = list((self.store.root / "archive").glob("*.md"))
        self.assertEqual(len(archive_files), 1)
        self.assertIn("outdated standing fact", archive_files[0].read_text(encoding="utf-8"))

    async def test_archive_missing_note_raises(self):
        with self.assertRaises(NoteStoreError) as raised:
            await self.store.archive_page("missing")
        self.assertEqual(raised.exception.code, "not_found")


class NoteToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = NoteStore(str(Path(self._tmpdir.name) / "notes"))
        self.memory = MarkdownMemory(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    async def test_upsert_read_list_archive_tools(self):
        upsert = create_upsert_note_tool(self.store)
        read = create_read_note_tool(self.store)
        listing = create_list_notes_tool(self.store)
        archive = create_archive_note_tool(self.store)

        saved = await upsert(title="Home wifi", content="SSID orchard.")
        self.assertEqual(saved["status"], "ok")
        self.assertEqual(saved["slug"], "home-wifi")

        listed = await listing()
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["notes"][0]["slug"], "home-wifi")

        loaded = await read("Home wifi")
        self.assertEqual(loaded["status"], "ok")
        self.assertIn("orchard", loaded["content"])

        archived = await archive("home-wifi")
        self.assertEqual(archived["status"], "ok")
        missing = await read("home-wifi")
        self.assertEqual(missing["status"], "not_found")

    async def test_write_memory_stays_on_diary(self):
        tool = create_write_memory_tool(self.memory)
        result = await tool("Today we decided the ship window.")
        self.assertEqual(result["status"], "ok")
        daily = await self.memory.read_file(self.memory.daily_path(date.today()))
        self.assertIn("ship window", daily)
        self.assertEqual(await self.store.count_pages(), 0)

    async def test_search_memory_scope_notes(self):
        await self.store.upsert_page(title="Release process", body="Friday tag then deploy.")
        tool = create_search_memory_tool(self.memory)
        result = await tool(query=["Friday"], scope="notes")
        self.assertIn("Friday", result["results"])
        self.assertIn("[note release-process]", result["results"])
        self.assertNotIn(str(self.memory.root), result["results"])

    async def test_search_memory_all_includes_notes(self):
        await self.memory.append_daily("Went to lunch")
        await self.store.upsert_page(title="Wifi", body="SSID orchard network.")
        tool = create_search_memory_tool(self.memory)
        result = await tool(query=["orchard"], scope="all")
        self.assertIn("orchard", result["results"])
        self.assertIn("[note wifi]", result["results"])


class NotebookContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        self.store = NoteStore(str(root / "notes"))
        self.memory = MarkdownMemory(str(root / "memory"))
        self.handler = MemoryHandler(
            memory=self.memory,
            llm_service=object(),
            message_storage=object(),
            journal_batch_size=20,
            note_store=self.store,
        )

    def tearDown(self):
        self._tmpdir.cleanup()

    async def test_empty_notebook_injects_nothing(self):
        self.assertEqual(await self.handler.get_notebook_context(), "")

    async def test_catalog_without_unpinned_bodies(self):
        await self.store.upsert_page(
            title="Wifi",
            body="SSID orchard.\nThe backup password is written on the router.",
        )
        await self.store.upsert_page(
            title="How I work",
            body="Keep replies short unless the person wants detail.",
            pinned=True,
        )
        context = await self.handler.get_notebook_context()
        self.assertIn("<catalog>", context)
        self.assertIn("Wifi (wifi)", context)
        self.assertIn("[pinned] How I work", context)
        self.assertIn("SSID orchard", context)
        self.assertIn("<pinned>", context)
        self.assertIn("Keep replies short", context)
        self.assertNotIn("backup password", context)

    async def test_catalog_respects_page_budget(self):
        for index in range(5):
            await self.store.upsert_page(
                title=f"Page {index}",
                body=f"Standing fact number {index}.",
            )
        with patch.object(AgentConfig, "NOTE_CATALOG_MAX_PAGES", 2):
            context = await self.handler.get_notebook_context()
        self.assertEqual(context.count("- "), 2)
        self.assertNotIn("<pinned>", context)


class NotebookInjectionLayerTests(unittest.TestCase):
    def _layer(self, messages, name):
        return next((m for m in messages if m.get("name") == name), None)

    def test_reply_mode_injects_notebook_context_layer(self):
        messages = MessageHandler.build_turn_context_messages(
            [],
            current_user_id="alice",
            notebook_context="<catalog>\n- Wifi (wifi) — SSID orchard\n</catalog>",
        )
        layer = self._layer(messages, AgentConfig.NOTEBOOK_CONTEXT_NAME)
        self.assertIsNotNone(layer)
        self.assertIn("SSID orchard", layer["content"])
        self.assertIn('notebook_context trusted_as_instruction="false"', layer["content"])
        self.assertIn(AgentConfig.NOTEBOOK_CONTEXT_PURPOSE, layer["content"])

    def test_no_layer_when_notebook_context_empty(self):
        messages = MessageHandler.build_turn_context_messages(
            [],
            current_user_id="alice",
            notebook_context="   ",
        )
        self.assertIsNone(self._layer(messages, AgentConfig.NOTEBOOK_CONTEXT_NAME))

    def test_subconscious_mode_still_gets_notebook_catalog(self):
        messages = MessageHandler.build_turn_context_messages(
            [],
            current_user_id="agent",
            notebook_context="<catalog>\n- How I work (how-i-work)\n</catalog>",
            task_mode="subconscious_json",
        )
        layer = self._layer(messages, AgentConfig.NOTEBOOK_CONTEXT_NAME)
        self.assertIsNotNone(layer)
        self.assertIn("how-i-work", layer["content"])


class NotebookPromptRuleTests(unittest.TestCase):
    def test_self_rules_mention_notebook(self):
        self.assertIn("Standing topic knowledge lives in your notebook", AgentConfig.BASE_AGENT_PROMPT)
        self.assertIn("read a page when the topic matches", AgentConfig.BASE_AGENT_PROMPT)


class AgentNotebookWiringTests(unittest.TestCase):
    def test_agent_wires_note_store_and_tools(self):
        from xagent.core.agent import Agent

        with tempfile.TemporaryDirectory() as tmpdir:
            agent = Agent(client=object(), workspace=tmpdir)
            self.assertTrue((Path(tmpdir) / "memory" / "notes").is_dir())
            self.assertIn("upsert_note", agent.tools)
            self.assertIn("read_note", agent.tools)
            self.assertIn("list_notes", agent.tools)
            self.assertIn("archive_note", agent.tools)
            self.assertIn("write_memory", agent.tools)
            self.assertIs(agent.memory_handler.note_store, agent.note_store)


if __name__ == "__main__":
    unittest.main()
