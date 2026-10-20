"""Tests for notebook memory: store, tools, and injection."""

import asyncio
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from xagent.components.memory import (
    MAX_BODY_CHARS,
    Note,
    NoteStore,
)
from xagent.core.config import AgentConfig
from xagent.core.handlers.memory import MemoryHandler
from xagent.core.handlers.message import MessageHandler
from xagent.schemas import Message, RoleType
from xagent.tools.note_tool import (
    create_read_note_tool,
    create_search_note_tool,
)


def _note(store, title, body, **kwargs):
    return NoteStore.normalize(Note(id=store.next_id(), title=title, body=body, **kwargs))


# ----------------------------------------------------------------------
# NoteStore: storage I/O
# ----------------------------------------------------------------------


class NoteStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = NoteStore(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    async def test_write_read_roundtrip_preserves_fields(self):
        await self.store.write(Note(id="202608190931", title="Brewing temperature", body="92C."))
        note = _note(
            self.store,
            "Jun takes espresso at 1:2.5",
            "Jun wants 1:2.5 at 92C; thinner has no spine to him.",
            keys=("espresso", "Jun"),
            links=("202608190931",),
            source={"diary": ["2026-08-19"]},
        )
        await self.store.write(note)

        loaded = await self.store.read(note.id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.title, "Jun takes espresso at 1:2.5")
        self.assertIn("no spine", loaded.body)
        self.assertEqual(loaded.keys, ("espresso", "Jun"))
        self.assertEqual(loaded.links, ("202608190931",))
        self.assertEqual(loaded.source, {"diary": ["2026-08-19"]})
        rendered = self.store.path_for(note.id).read_text(encoding="utf-8")
        self.assertNotIn("status:", rendered)
        self.assertFalse(hasattr(loaded, "status"))

    async def test_id_stays_a_string_through_yaml_roundtrip(self):
        note = _note(self.store, "Leading zeros survive", "body")
        await self.store.write(note)
        loaded = await self.store.read(note.id)
        self.assertIsInstance(loaded.id, str)
        self.assertEqual(loaded.id, note.id)

    async def test_filename_carries_slug_but_id_owns_identity(self):
        note = _note(self.store, "Grinder reads two clicks coarse", "body")
        await self.store.write(note)
        path = self.store.path_for(note.id)
        self.assertTrue(path.name.startswith(note.id))
        self.assertIn("grinder-reads", path.name)

        renamed = NoteStore.normalize(
            Note(id=note.id, title="Something else entirely", body="body")
        )
        await self.store.write(renamed)
        self.assertEqual(self.store.path_for(note.id), path)
        self.assertEqual(len(list(Path(self._tmpdir.name).glob("*.md"))), 1)

    async def test_cjk_only_title_yields_id_only_filename(self):
        note = _note(self.store, "浓缩粉水比", "固定 1:2.5")
        await self.store.write(note)
        self.assertEqual(self.store.path_for(note.id).name, f"{note.id}.md")
        loaded = await self.store.read(note.id)
        self.assertEqual(loaded.title, "浓缩粉水比")

    async def test_create_assigns_distinct_ids_under_concurrency(self):
        created = await asyncio.gather(
            *(
                self.store.create(Note(id="", title=f"Note {index}", body="body"))
                for index in range(5)
            )
        )
        self.assertEqual(len({note.id for note in created}), 5)
        self.assertEqual(await self.store.count(), 5)

    def test_next_id_walks_forward_on_collision(self):
        stamp = datetime(2026, 8, 19, 9, 30)
        first = self.store.next_id(stamp)
        self.assertEqual(first, "202608190930")
        (Path(self._tmpdir.name) / f"{first}-taken.md").write_text("x", encoding="utf-8")
        self.store._cache_signature = None
        self.assertEqual(self.store.next_id(stamp), "202608190931")

    async def test_parse_tolerates_broken_frontmatter(self):
        path = Path(self._tmpdir.name) / "202608190930-damaged.md"
        path.write_text(
            "---\ntitle: [unclosed\n---\n\nI still want this note kept.\n",
            encoding="utf-8",
        )
        notes = await self.store.list_notes()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].id, "202608190930")
        self.assertIn("still want this note kept", notes[0].body)

    async def test_parse_tolerates_missing_frontmatter(self):
        path = Path(self._tmpdir.name) / "202608190931-bare.md"
        path.write_text("Just a body someone typed by hand.\n", encoding="utf-8")
        notes = await self.store.list_notes()
        self.assertEqual([note.id for note in notes], ["202608190931"])
        self.assertEqual(notes[0].title, "Just a body someone typed by hand.")

    async def test_new_format_contains_only_notebook_fields(self):
        note = await self.store.create(Note(id="", title="Offline storage", body="Use SQLite.",
                                            keys=("SQLite",), source={"diary": ["2026-10-20"]}))
        rendered = self.store.path_for(note.id).read_text()
        self.assertNotIn("created:", rendered)
        self.assertEqual((await self.store.read(note.id)).source, {"diary": ["2026-10-20"]})

    async def test_normalize_clamps_body_and_keys(self):
        note = NoteStore.normalize(
            Note(
                id="202608190930",
                title="t" * 200,
                body="b" * (MAX_BODY_CHARS + 500),
                keys=("ok", "x", "  ", "ok"),
                links=("202608190931", "nope", "202608190931"),
            )
        )
        self.assertEqual(len(note.title), 80)
        self.assertEqual(len(note.body), MAX_BODY_CHARS)
        self.assertEqual(note.keys, ("ok",))
        self.assertEqual(note.links, ("202608190931",))

    async def test_inline_wiki_links_are_collected(self):
        target = _note(self.store, "Target", "body")
        await self.store.write(target)
        hub = _note(self.store, "Hub", f"See [[{target.id}]] for the ratio.")
        await self.store.write(hub)

        loaded = await self.store.read(hub.id)
        self.assertIn(target.id, loaded.links)
        backlinks = await self.store.backlinks(target.id)
        self.assertEqual([note.id for note in backlinks], [hub.id])

    async def test_neighbours_returns_links_then_backlinks(self):
        linked = _note(self.store, "Linked", "body")
        await self.store.write(linked)
        subject = _note(self.store, "Subject", "body", links=(linked.id,))
        await self.store.write(subject)
        linking = _note(self.store, "Linking", "body", links=(subject.id,))
        await self.store.write(linking)

        neighbours = await self.store.neighbours(await self.store.read(subject.id))
        self.assertEqual([note.id for note in neighbours], [linked.id, linking.id])

    async def test_cache_invalidates_when_a_note_changes_on_disk(self):
        note = _note(self.store, "First", "body")
        await self.store.write(note)
        self.assertEqual(len(await self.store.list_notes()), 1)

        second = _note(self.store, "Second", "body")
        await self.store.write(second)
        self.assertEqual(len(await self.store.list_notes()), 2)

        path = self.store.path_for(note.id)
        path.write_text(
            path.read_text(encoding="utf-8").replace("First", "Rewritten"),
            encoding="utf-8",
        )
        titles = {item.title for item in await self.store.list_notes()}
        self.assertIn("Rewritten", titles)


# ----------------------------------------------------------------------
# NoteStore: retrieval
# ----------------------------------------------------------------------


class NoteRetrievalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = NoteStore(self._tmpdir.name)
        self.espresso = _note(
            self.store,
            "Jun 的浓缩固定 1:2.5",
            "Jun 喝浓缩要 1:2.5、92 度，再淡他说没有骨架。",
            keys=("浓缩", "Jun"),
        )
        await self.store.write(self.espresso)
        self.grinder = _note(
            self.store,
            "Grinder reads two clicks coarse",
            "The home grinder is offset by two clicks; dial finer than the recipe says.",
            keys=("grinder",),
        )
        await self.store.write(self.grinder)

    async def asyncTearDown(self):
        self._tmpdir.cleanup()

    async def test_recall_matches_chinese_without_a_tokenizer(self):
        recalled = await self.store.recall("今天想给 Jun 冲个浓缩，粉水比多少")
        self.assertEqual([note.id for note in recalled], [self.espresso.id])

    async def test_recall_matches_english_keys(self):
        recalled = await self.store.recall("is the grinder still off?")
        self.assertEqual([note.id for note in recalled], [self.grinder.id])

    async def test_recall_ranks_more_key_hits_first(self):
        recalled = await self.store.recall("Jun 的浓缩和 grinder 都要调")
        self.assertEqual(recalled[0].id, self.espresso.id)

    async def test_recall_returns_nothing_for_unrelated_text(self):
        self.assertEqual(await self.store.recall("what is the train timetable"), [])

    async def test_recall_skips_keys_below_minimum_length(self):
        short = _note(self.store, "Short key", "body", keys=("a",))
        await self.store.write(short)
        self.assertEqual(await self.store.recall("a a a"), [])

    async def test_search_scores_title_above_body(self):
        results = await self.store.search(["grinder"])
        self.assertEqual(results[0].id, self.grinder.id)

    async def test_search_with_empty_query_browses_by_recency(self):
        results = await self.store.search([])
        self.assertEqual({note.id for note in results}, {self.espresso.id, self.grinder.id})

# ----------------------------------------------------------------------
# Note tools
# ----------------------------------------------------------------------


class NoteToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = NoteStore(self._tmpdir.name)
        self.search_note = create_search_note_tool(self.store)
        self.read_note = create_read_note_tool(self.store)

    def tearDown(self):
        self._tmpdir.cleanup()

    async def test_search_is_bounded_summary_and_read_returns_full_note_and_source(self):
        body = "SQLite works offline. " + "Detailed rationale. " * 40
        note = await self.store.create(Note(id="", title="Offline storage", body=body,
                                            keys=("SQLite",), source={"diary": ["2026-10-20"]}))
        results = await self.search_note(query=["SQLite"])
        row = results["notes"][0]
        self.assertEqual(set(row), {"id", "title", "snippet"})
        self.assertLess(len(row["snippet"]), len(body))
        opened = await self.read_note(row["id"])
        self.assertEqual(opened["note"]["body"], body.strip())
        self.assertEqual(opened["note"]["source"], note.source)

    async def test_read_shows_both_directions_once_and_never_recurses(self):
        target = await self.store.create(Note(id="", title="Target", body="Conclusion."))
        other = await self.store.create(Note(id="", title="Other", body="Application.", links=(target.id,)))
        await self.store.write(Note(id=target.id, title=target.title, body=target.body,
                                    links=(other.id, target.id, "999999999999")))
        for index in range(4):
            await self.store.create(Note(id="", title=f"Incoming {index}", body="Support.", links=(target.id,)))
        opened = await self.read_note(target.id)
        self.assertEqual(opened["note"]["links"], [other.id])
        self.assertEqual(len(opened["neighbours"]), 3)
        both = next(row for row in opened["neighbours"] if row["id"] == other.id)
        self.assertEqual(both["direction"], "both")
        self.assertTrue(all("body" not in row and "neighbours" not in row for row in opened["neighbours"]))
        self.assertEqual(len({row["id"] for row in opened["neighbours"]}), 3)

    async def test_reference_changes_update_backlinks_and_deletion_is_safe(self):
        target = await self.store.create(Note(id="", title="Target", body="Conclusion."))
        source = await self.store.create(Note(id="", title="Source", body=f"Applies [[{target.id}]]."))
        opened = await self.read_note(target.id)
        self.assertEqual(opened["neighbours"][0]["direction"], "incoming")
        await self.store.write(Note(id=source.id, title="Renamed", body="Independent conclusion."))
        self.assertEqual((await self.read_note(target.id))["neighbours"], [])
        await self.store.delete(target.id)
        self.assertEqual((await self.read_note(target.id))["status"], "not_found")

    async def test_empty_search_browses_and_limit_is_enforced(self):
        for index in range(4):
            await self.store.create(Note(id="", title=f"Topic {index}", body="Conclusion."))
        self.assertEqual(len((await self.search_note(limit=2))["notes"]), 2)


# ----------------------------------------------------------------------
# MemoryHandler: notebook injection (no background distillation)
# ----------------------------------------------------------------------


class _FakeDiaryLLMService:
    """Stub journal service for notebook injection tests."""

    def __init__(self, summary_text="Weekly recap"):
        self.summary_text = summary_text
        self.summary_calls = []

    async def format_diary_entry(self, messages, journal_date, existing_today=""):
        return "\n".join(
            str(message.get("content", "")) for message in messages if message.get("content")
        )

    async def generate_summary(self, source_content, period_type, period_label):
        self.summary_calls.append((source_content, period_type, period_label))
        return self.summary_text

    async def update_memory_projections(self, **kwargs):
        return {}


class _FakeMessageStorage:
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

    async def get_messages(self, count=20, offset=0):
        return self.messages[-count:] if count > 0 else []


class MemoryHandlerNotebookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from xagent.components.memory import MarkdownMemory

        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        self.notes = NoteStore(str(root / "notes"))
        self.memory = MarkdownMemory(str(root / "memory"))

    def tearDown(self):
        self._tmpdir.cleanup()

    def _make_handler(self, storage, llm, diary_write_batch=20):
        return MemoryHandler(
            memory=self.memory,
            llm_service=llm,
            message_storage=storage,
            diary_write_batch=diary_write_batch,
            note_store=self.notes,
        )

    def _batch(self, count=20, sender="jun", channel="weixin"):
        messages = [
            Message.create(content=f"message {index}", role=RoleType.USER, sender_id=sender)
            for index in range(count)
        ]
        for message in messages:
            message.channel = channel
        return messages


    async def test_no_change_projection_keeps_notebook_empty(self):
        storage = _FakeMessageStorage(self._batch())
        llm = _FakeDiaryLLMService()
        handler = self._make_handler(storage, llm)

        self.assertTrue(await handler.run_maintenance(force=True))
        self.assertEqual(await self.notes.count(), 0)
        recent = await handler.get_recent_context()
        self.assertIn("message 0", recent)


    async def test_weekly_summary_does_not_distil_notes(self):
        llm = _FakeDiaryLLMService()
        handler = self._make_handler(_FakeMessageStorage(), llm)
        week_start = date(2026, 8, 11)
        week_end = date(2026, 8, 17)
        await self.memory.append_daily("Espresso week", target_date=week_start)

        self.assertTrue(await handler._generate_weekly(week_start, week_end))
        self.assertEqual(await self.notes.count(), 0)
        summary = await self.memory.read_file(self.memory.weekly_path(week_start, week_end))
        self.assertEqual(summary, "Weekly recap")

    async def test_monthly_summary_does_not_garden_notes(self):
        for index in range(4):
            await self.notes.write(
                _note(
                    self.notes,
                    f"Coffee idea {index}",
                    f"Detail about coffee idea {index}.",
                    keys=(f"coffee{index}",),
                )
            )
        handler = self._make_handler(_FakeMessageStorage(), _FakeDiaryLLMService())
        await self.memory.append_daily("Coffee month", target_date=date(2026, 8, 1))

        self.assertTrue(await handler._generate_monthly(2026, 8))
        notes = await self.notes.list_notes()
        self.assertEqual(len(notes), 4)
        self.assertTrue(all(not note.links for note in notes))

    async def test_notebook_context_is_empty_without_matching_notes(self):
        handler = MemoryHandler(
            memory=self.memory,
            llm_service=_FakeDiaryLLMService(),
            message_storage=_FakeMessageStorage(),
            diary_write_batch=20,
        )
        self.assertEqual(await handler.get_notebook_context("anything"), "")

    async def test_notebook_context_injects_recalled_notes_only(self):
        await self.notes.write(
            _note(self.notes, "Grinder is two clicks coarse", "Dial finer.", keys=("grinder",))
        )
        await self.notes.write(
            _note(self.notes, "Jun 的浓缩固定 1:2.5", "再淡他说没有骨架。", keys=("浓缩",))
        )
        handler = self._make_handler(_FakeMessageStorage(), _FakeDiaryLLMService())

        context = await handler.get_notebook_context("给 Jun 冲个浓缩")
        self.assertNotIn("[pinned]", context)
        self.assertNotIn("[hubs]", context)
        self.assertNotIn("[relevant to the current message]", context)
        self.assertNotIn("Grinder is two clicks coarse", context)
        self.assertIn("浓缩", context)
        self.assertIn("再淡他说没有骨架。", context)

    async def test_notebook_context_is_empty_without_a_message(self):
        await self.notes.write(
            _note(self.notes, "Jun 的浓缩固定 1:2.5", "body", keys=("浓缩",))
        )
        handler = self._make_handler(_FakeMessageStorage(), _FakeDiaryLLMService())
        self.assertEqual(await handler.get_notebook_context(""), "")

    async def test_notebook_context_caps_recalled_notes(self):
        for index in range(6):
            await self.notes.write(
                _note(self.notes, f"Loose {index}", "short", keys=(f"loose{index}",))
            )
        handler = self._make_handler(_FakeMessageStorage(), _FakeDiaryLLMService())

        context = await handler.get_notebook_context(
            " ".join(f"loose{index}" for index in range(6))
        )
        self.assertEqual(context.count("- ("), AgentConfig.NOTEBOOK_RECALL_MAX)

    async def test_notebook_context_stays_within_the_prompt_budget(self):
        notes = [
            NoteStore.normalize(
                Note(
                    id=f"20260819093{index}",
                    title=f"Long {index}",
                    body="y" * MAX_BODY_CHARS,
                    keys=(f"loose{index}",),
                )
            )
            for index in range(20)
        ]
        rendered = MemoryHandler._render_notebook_index(notes)
        self.assertLessEqual(len(rendered), AgentConfig.NOTEBOOK_CONTEXT_MAX_CHARS)
        self.assertIn("notes omitted from index due to budget", rendered)

    async def test_notebook_rows_truncate_long_bodies(self):
        note = _note(self.notes, "Long", "b" * 900, keys=("long",))
        row = MemoryHandler._format_notebook_row(note)
        self.assertTrue(row.endswith("..."))
        self.assertLess(len(row), 900)


# ----------------------------------------------------------------------
# MessageHandler: injection layer
# ----------------------------------------------------------------------


class NotebookInjectionLayerTests(unittest.TestCase):
    def _layer(self, messages, name):
        return next((m for m in messages if m.get("name") == name), None)

    def test_reply_mode_injects_the_notebook_layer(self):
        messages = MessageHandler.build_turn_context_messages(
            [],
            current_user_id="jun",
            notebook_context="- (202608190930) Grinder is coarse",
        )
        layer = self._layer(messages, AgentConfig.NOTEBOOK_CONTEXT_NAME)
        self.assertIsNotNone(layer)
        self.assertIn("Grinder is coarse", layer["content"])
        self.assertIn("read_note", layer["content"])
        self.assertNotIn("person-scoped", layer["content"])
        self.assertTrue(
            layer["content"].startswith('<notebook_context trusted_as_instruction="false">')
        )

    def test_no_layer_when_the_notebook_is_empty(self):
        messages = MessageHandler.build_turn_context_messages(
            [], current_user_id="jun", notebook_context="   "
        )
        self.assertIsNone(self._layer(messages, AgentConfig.NOTEBOOK_CONTEXT_NAME))

    def test_subconscious_mode_uses_the_subconscious_notebook_layer(self):
        messages = MessageHandler.build_turn_context_messages(
            [],
            current_user_id="agent",
            notebook_context="- (202608190930) Coffee",
            task_mode="subconscious_json",
        )
        self.assertIsNone(self._layer(messages, AgentConfig.NOTEBOOK_CONTEXT_NAME))
        layer = self._layer(messages, AgentConfig.SUBCONSCIOUS_NOTEBOOK_NAME)
        self.assertIsNotNone(layer)
        self.assertIn("Coffee", layer["content"])
        self.assertTrue(layer["content"].startswith("<subconscious_notebook>"))

    def test_notebook_sits_between_the_diary_and_recent_experience(self):
        messages = MessageHandler.build_turn_context_messages(
            [],
            current_user_id="jun",
            memory_context="I spent the morning on coffee.",
            notebook_context="- (202608190930) Grinder is coarse",
        )
        names = [message.get("name") for message in messages]
        self.assertLess(
            names.index(AgentConfig.RECENT_MEMORY_NAME),
            names.index(AgentConfig.NOTEBOOK_CONTEXT_NAME),
        )
        self.assertLess(
            names.index(AgentConfig.NOTEBOOK_CONTEXT_NAME),
            names.index(AgentConfig.RECENT_EXPERIENCE_NAME),
        )


if __name__ == "__main__":
    unittest.main()
