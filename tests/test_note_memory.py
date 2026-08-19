"""Tests for topic-indexed standing notes (store, write tool, search)."""

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from xagent.components.memory import MarkdownMemory, NoteStore, NoteStoreError, slugify
from xagent.components.memory.note_memory import NOTE_BODY_MAX_CHARS, extract_wiki_links
from xagent.core.config import AgentConfig
from xagent.core.handlers.memory import MemoryHandler
from xagent.core.journal import JournalLLMService
from xagent.schemas import Message, RoleType
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
            self.assertIs(agent.memory_handler.note_store, agent.note_store)


class StandingNoteParseTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_standing_notes_object_and_list(self):
        parsed = JournalLLMService._parse_standing_notes(
            '{"notes": [{"slug": "fire", "title": "Fire", "body": "Heat first."}]}'
        )
        self.assertEqual(parsed, [{"slug": "fire", "title": "Fire", "body": "Heat first."}])
        as_list = JournalLLMService._parse_standing_notes(
            '[{"title": "Fire", "body": "Heat first."}]'
        )
        self.assertEqual(as_list, [{"slug": "", "title": "Fire", "body": "Heat first."}])

    def test_parse_standing_notes_strips_code_fences(self):
        parsed = JournalLLMService._parse_standing_notes(
            '```json\n{"notes": [{"slug": "fire", "title": "Fire", "body": "Heat."}]}\n```'
        )
        self.assertEqual(parsed[0]["slug"], "fire")

    def test_parse_standing_notes_handles_bad_json_and_empty(self):
        self.assertEqual(JournalLLMService._parse_standing_notes("not json"), [])
        self.assertEqual(JournalLLMService._parse_standing_notes('{"notes": []}'), [])
        self.assertEqual(JournalLLMService._parse_standing_notes('{"notes": [{"title": "x"}]}'), [])

    async def test_update_standing_notes_calls_model(self):
        service = JournalLLMService(client=object())
        captured = {}

        async def fake_call_text(system_prompt, user_prompt):
            captured["system"] = system_prompt
            captured["user"] = user_prompt
            return '{"notes": [{"slug": "fire", "title": "Fire", "body": "Heat first."}]}'

        service._call_text = fake_call_text  # type: ignore[assignment]
        result = await service.update_standing_notes(
            messages=[{"role": "user", "sender_id": "a", "content": "we cook with fire"}],
            catalog=[{"slug": "fire", "title": "Fire", "summary": "Heat first."}],
            working_set=[{"slug": "fire", "title": "Fire", "body": "Old heat."}],
        )
        self.assertEqual(result[0]["slug"], "fire")
        self.assertIn("one idea per page", captured["system"])
        self.assertIn('slug="fire"', captured["user"])
        self.assertIn("we cook with fire", captured["user"])

    async def test_update_standing_notes_noops_without_messages(self):
        service = JournalLLMService(client=object())
        result = await service.update_standing_notes(messages=[], catalog=[], working_set=[])
        self.assertEqual(result, [])


class _FakeNoteMaintenanceLLM:
    def __init__(self, notes=None, error=None):
        self.notes = list(notes or [])
        self.error = error
        self.note_calls = []
        self.diary_calls = []

    async def format_diary_entry(self, messages, journal_date):
        self.diary_calls.append({"journal_date": journal_date, "messages": list(messages)})
        return "diary from batch"

    async def generate_summary(self, source_content, period_type, period_label):
        return ""

    async def update_relationship_cards(self, participants, messages, existing_cards):
        return {}

    async def update_standing_notes(self, messages, catalog, working_set):
        self.note_calls.append(
            {"messages": messages, "catalog": catalog, "working_set": working_set}
        )
        if self.error is not None:
            raise self.error
        return list(self.notes)


class _FakeNoteMessageStorage:
    def __init__(self, messages=None):
        self.messages = list(messages or [])

    async def get_latest_message_cursor(self):
        return len(self.messages)

    async def get_messages_in_cursor_range(self, start_exclusive=0, end_inclusive=None):
        start = max(0, int(start_exclusive or 0))
        end = len(self.messages) if end_inclusive is None else max(0, int(end_inclusive))
        if end <= start:
            return []
        return self.messages[start:end]


class MemoryHandlerNoteMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        self.store = NoteStore(str(root / "notes"))
        self.memory = MarkdownMemory(str(root / "memory"))

    def tearDown(self):
        self._tmpdir.cleanup()

    def _messages(self, count=8, content="we talked about cooking with fire"):
        messages = [
            Message.create(content=content if index == 0 else f"filler {index}", role=RoleType.USER, sender_id="alice")
            for index in range(count)
        ]
        for message in messages:
            message.channel = "api"
        return messages

    def _handler(self, storage, llm, journal_batch_size=8):
        return MemoryHandler(
            memory=self.memory,
            llm_service=llm,
            message_storage=storage,
            journal_batch_size=journal_batch_size,
            note_store=self.store,
        )

    def test_working_set_prefers_transcript_hits_then_recency(self):
        from types import SimpleNamespace

        pages = [
            SimpleNamespace(slug="old", title="Old", body="unrelated", touched="2026-01-01", updated="2026-01-01"),
            SimpleNamespace(slug="hit", title="Fire", body="cooking with heat", touched="2026-01-02", updated="2026-01-02"),
            SimpleNamespace(slug="recent", title="Recent", body="misc", touched="2026-08-19", updated="2026-08-19"),
        ]
        selected = MemoryHandler._select_note_working_set(pages, "cooking with fire", 2)
        self.assertEqual([page.slug for page in selected], ["hit", "recent"])

    async def test_empty_notebook_can_create_first_page(self):
        llm = _FakeNoteMaintenanceLLM(
            notes=[{"slug": "fire", "title": "Fire", "body": "Cooking starts with heat."}]
        )
        handler = self._handler(_FakeNoteMessageStorage(self._messages()), llm)
        wrote = await handler.run_maintenance(force=True)
        self.assertTrue(wrote)
        self.assertEqual(len(llm.note_calls), 1)
        page = await self.store.read_page("fire")
        self.assertIsNotNone(page)
        self.assertIn("Cooking starts with heat", page.body)

    async def test_orphan_second_page_is_rejected(self):
        await self.store.upsert_page(title="Fire", body="Cooking starts with heat.")
        llm = _FakeNoteMaintenanceLLM(
            notes=[{"slug": "unattached", "title": "Unattached", "body": "A second idea with no links."}]
        )
        handler = self._handler(_FakeNoteMessageStorage(self._messages()), llm)
        wrote = await handler.run_maintenance(force=True)
        self.assertTrue(wrote)
        self.assertIsNone(await self.store.read_page("unattached"))
        self.assertEqual(await self.store.count_pages(), 1)

    async def test_linked_new_page_is_written(self):
        await self.store.upsert_page(title="Fire", body="Cooking starts with heat.")
        llm = _FakeNoteMaintenanceLLM(
            notes=[{
                "slug": "weeknight-cooking",
                "title": "Weeknight cooking",
                "body": "Keep it simple. Heat lives on [[fire]].",
            }]
        )
        handler = self._handler(_FakeNoteMessageStorage(self._messages()), llm)
        await handler.run_maintenance(force=True)
        page = await self.store.read_page("weeknight-cooking")
        self.assertIsNotNone(page)
        self.assertEqual(page.links, ["fire"])

    async def test_maintenance_caps_writes_per_batch(self):
        llm = _FakeNoteMaintenanceLLM(
            notes=[
                {"slug": "n0", "title": "N0", "body": "First standing idea."},
                {"slug": "n1", "title": "N1", "body": "Second idea. See [[n0]]."},
                {"slug": "n2", "title": "N2", "body": "Third idea. See [[n0]]."},
                {"slug": "n3", "title": "N3", "body": "Fourth idea. See [[n0]]."},
            ]
        )
        handler = self._handler(_FakeNoteMessageStorage(self._messages()), llm)
        await handler.run_maintenance(force=True)
        self.assertEqual(await self.store.count_pages(), AgentConfig.NOTE_MAINTENANCE_MAX_WRITES)
        self.assertIsNone(await self.store.read_page("n3"))

    async def test_unseen_existing_slug_is_not_overwritten(self):
        await self.store.upsert_page(title="Hidden idea", body="unique-hidden-token standing fact")
        for index in range(8):
            await self.store.upsert_page(title=f"Recent {index}", body=f"recent body {index} cooking")
        original = await self.store.read_page("hidden-idea")
        llm = _FakeNoteMaintenanceLLM(
            notes=[{
                "slug": "hidden-idea",
                "title": "Hidden idea",
                "body": "should not replace the hidden page [[recent-0]].",
            }]
        )
        handler = self._handler(_FakeNoteMessageStorage(self._messages()), llm)
        await handler.run_maintenance(force=True)
        working_slugs = {item["slug"] for item in llm.note_calls[0]["working_set"]}
        self.assertNotIn("hidden-idea", working_slugs)
        loaded = await self.store.read_page("hidden-idea")
        self.assertEqual(loaded.body, original.body)

    async def test_note_update_failure_still_commits_diary(self):
        llm = _FakeNoteMaintenanceLLM(error=RuntimeError("notes failed"))
        storage = _FakeNoteMessageStorage(self._messages())
        handler = self._handler(storage, llm)
        wrote = await handler.run_maintenance(force=True)
        self.assertTrue(wrote)
        self.assertEqual(len(llm.note_calls), 1)
        daily = await self.memory.read_file(self.memory.daily_path(date.today()))
        self.assertIn("diary from batch", daily)
        self.assertGreater(handler._last_processed_message_id, 0)
        self.assertEqual(await self.store.count_pages(), 0)


if __name__ == "__main__":
    unittest.main()
