"""Tests for notebook memory: store, tools, distillation, injection."""

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from xagent.components.memory import (
    KIND_HUB,
    MAX_BODY_CHARS,
    STATUS_ARCHIVED,
    Note,
    NoteStore,
)
from xagent.core.config import AgentConfig
from xagent.core.handlers.memory import MemoryHandler
from xagent.core.handlers.message import MessageHandler
from xagent.core.journal import JournalLLMService
from xagent.schemas import Message, RoleType
from xagent.tools.note_tool import (
    create_read_note_tool,
    create_search_note_tool,
    create_update_note_tool,
    create_write_note_tool,
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
        note = _note(
            self.store,
            "Jun takes espresso at 1:2.5",
            "Jun wants 1:2.5 at 92C; thinner has no spine to him.",
            tags=("coffee", "preference"),
            keys=("espresso", "Jun"),
            sensitivity="person-scoped",
            source={"diary": ["2026-08-19"], "person": "feishu:ou_a", "cursor": 42},
        )
        await self.store.write(note)

        loaded = await self.store.read(note.id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.title, "Jun takes espresso at 1:2.5")
        self.assertIn("no spine", loaded.body)
        self.assertEqual(loaded.tags, ("coffee", "preference"))
        self.assertEqual(loaded.keys, ("espresso", "Jun"))
        self.assertEqual(loaded.sensitivity, "person-scoped")
        self.assertEqual(loaded.source["person"], "feishu:ou_a")
        self.assertEqual(loaded.source["cursor"], 42)

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

    async def test_normalize_clamps_body_tags_keys_and_enums(self):
        note = NoteStore.normalize(
            Note(
                id="202608190930",
                title="t" * 200,
                body="b" * (MAX_BODY_CHARS + 500),
                kind="nonsense",
                status="nonsense",
                sensitivity="nonsense",
                tags=tuple(f"tag{index}" for index in range(9)),
                keys=("ok", "x", "  ", "ok"),
                links=("202608190931", "nope", "202608190931"),
            )
        )
        self.assertEqual(len(note.title), 80)
        self.assertEqual(len(note.body), MAX_BODY_CHARS)
        self.assertEqual(note.kind, "note")
        self.assertEqual(note.status, "active")
        self.assertEqual(note.sensitivity, "shareable")
        self.assertEqual(len(note.tags), 5)
        self.assertEqual(note.keys, ("ok",))
        self.assertEqual(note.links, ("202608190931",))

    async def test_inline_wiki_links_are_collected(self):
        target = _note(self.store, "Target", "body")
        await self.store.write(target)
        hub = _note(self.store, "Hub", f"See [[{target.id}]] for the ratio.", kind=KIND_HUB)
        await self.store.write(hub)

        loaded = await self.store.read(hub.id)
        self.assertIn(target.id, loaded.links)
        backlinks = await self.store.backlinks(target.id)
        self.assertEqual([note.id for note in backlinks], [hub.id])

    async def test_archive_keeps_the_file_but_hides_the_note(self):
        note = _note(self.store, "No longer true", "body")
        await self.store.write(note)

        archived = await self.store.archive(note.id)
        self.assertEqual(archived.status, STATUS_ARCHIVED)
        self.assertEqual(await self.store.list_notes(), [])
        self.assertEqual(len(await self.store.list_notes(include_archived=True)), 1)
        self.assertTrue(self.store.path_for(note.id).exists())

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
            tags=("coffee",),
        )
        await self.store.write(self.espresso)
        self.grinder = _note(
            self.store,
            "Grinder reads two clicks coarse",
            "The home grinder is offset by two clicks; dial finer than the recipe says.",
            keys=("grinder",),
            tags=("coffee",),
            pinned=True,
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

    async def test_recall_ignores_archived_notes(self):
        await self.store.archive(self.grinder.id)
        self.assertEqual(await self.store.recall("is the grinder still off?"), [])

    async def test_recall_skips_keys_below_minimum_length(self):
        short = _note(self.store, "Short key", "body", keys=("a",))
        await self.store.write(short)
        self.assertEqual(await self.store.recall("a a a"), [])

    async def test_search_scores_title_above_body(self):
        results = await self.store.search(["grinder"])
        self.assertEqual(results[0].id, self.grinder.id)

    async def test_search_filters_by_tag_and_kind(self):
        hub = _note(self.store, "Coffee", "entry point", kind=KIND_HUB, tags=("coffee",))
        await self.store.write(hub)
        self.assertEqual(
            [note.id for note in await self.store.search([], kind=KIND_HUB)],
            [hub.id],
        )
        self.assertEqual(len(await self.store.search([], tags=["coffee"])), 3)
        self.assertEqual(await self.store.search([], tags=["missing"]), [])

    async def test_find_similar_surfaces_an_existing_note_on_the_same_idea(self):
        similar = await self.store.find_similar(
            title="浓缩的粉水比", keys=["浓缩"], tags=["coffee"]
        )
        self.assertEqual(similar[0].id, self.espresso.id)

    async def test_identity_score_ignores_a_passing_body_mention(self):
        mentions = _note(
            self.store,
            "Kitchen inventory",
            "We are low on beans, and the grinder needs descaling.",
            keys=("inventory",),
        )
        await self.store.write(mentions)

        self.assertEqual(
            NoteStore.identity_score(mentions, "grinder offset", keys=["grinder"]), 0
        )
        self.assertGreater(
            NoteStore.identity_score(self.grinder, "grinder offset", keys=["grinder"]), 0
        )

    async def test_pinned_and_hubs_are_scoped_to_their_kind(self):
        hub = _note(self.store, "Coffee", "entry point", kind=KIND_HUB)
        await self.store.write(hub)
        self.assertEqual([note.id for note in await self.store.pinned()], [self.grinder.id])
        self.assertEqual([note.id for note in await self.store.hubs()], [hub.id])


# ----------------------------------------------------------------------
# Note tools
# ----------------------------------------------------------------------


class NoteToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = NoteStore(self._tmpdir.name)
        self.write_note = create_write_note_tool(self.store)
        self.update_note = create_update_note_tool(self.store)
        self.search_note = create_search_note_tool(self.store)
        self.read_note = create_read_note_tool(self.store)

    def tearDown(self):
        self._tmpdir.cleanup()

    async def test_write_note_stores_a_note(self):
        result = await self.write_note(
            title="Jun takes espresso at 1:2.5",
            body="Jun wants 1:2.5 at 92C.",
            keys=["espresso", "Jun"],
            tags=["coffee"],
            sensitivity="person-scoped",
        )
        self.assertEqual(result["status"], "ok")
        stored = await self.store.read(result["note"]["id"])
        self.assertEqual(stored.sensitivity, "person-scoped")
        self.assertEqual(stored.source["diary"][0], stored.created)

    async def test_write_note_requires_title_and_body(self):
        self.assertEqual((await self.write_note(title="", body="x"))["status"], "skipped")
        self.assertEqual((await self.write_note(title="x", body="  "))["status"], "skipped")

    async def test_write_note_rejects_a_body_beyond_the_atomicity_cap(self):
        result = await self.write_note(title="Too much", body="x" * (MAX_BODY_CHARS + 1))
        self.assertEqual(result["status"], "too_long")
        self.assertEqual(await self.store.count(), 0)

    async def test_write_note_reports_an_existing_note_on_the_same_idea(self):
        first = await self.write_note(
            title="Jun takes espresso at 1:2.5",
            body="Jun wants 1:2.5 at 92C.",
            keys=["espresso", "Jun"],
            tags=["coffee"],
        )
        duplicate = await self.write_note(
            title="Jun espresso 1:2.5 preference",
            body="Jun likes it at 1:2.5.",
            keys=["espresso", "Jun"],
            tags=["coffee"],
        )
        self.assertEqual(duplicate["status"], "similar_exists")
        self.assertEqual(duplicate["candidates"][0]["id"], first["note"]["id"])
        self.assertEqual(await self.store.count(), 1)

    async def test_write_note_allows_a_genuinely_different_note(self):
        await self.write_note(title="Espresso ratio", body="1:2.5", keys=["espresso"])
        other = await self.write_note(
            title="Train to Hangzhou leaves at 7",
            body="The early train is the only one that connects.",
            keys=["train", "Hangzhou"],
        )
        self.assertEqual(other["status"], "ok")
        self.assertEqual(await self.store.count(), 2)

    async def test_update_note_changes_only_given_fields(self):
        created = await self.write_note(
            title="Espresso ratio", body="1:2.5", keys=["espresso"], tags=["coffee"]
        )
        note_id = created["note"]["id"]

        result = await self.update_note(note_id=note_id, body="1:2.5 at 92C", pinned=True)
        self.assertEqual(result["status"], "ok")
        stored = await self.store.read(note_id)
        self.assertEqual(stored.body, "1:2.5 at 92C")
        self.assertEqual(stored.title, "Espresso ratio")
        self.assertEqual(stored.tags, ("coffee",))
        self.assertTrue(stored.pinned)

    async def test_update_note_can_archive(self):
        created = await self.write_note(title="Stale", body="not true anymore")
        result = await self.update_note(note_id=created["note"]["id"], archive=True)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(await self.store.count(), 0)

    async def test_update_note_reports_missing_notes(self):
        result = await self.update_note(note_id="202608190930", body="x")
        self.assertEqual(result["status"], "not_found")

    async def test_search_note_returns_whole_notes(self):
        await self.write_note(
            title="Grinder reads two clicks coarse",
            body="Dial finer than the recipe says.",
            keys=["grinder"],
        )
        result = await self.search_note(query=["grinder"])
        self.assertEqual(result["total"], 1)
        self.assertIn("Dial finer", result["notes"][0]["body"])

    async def test_read_note_follows_links_when_asked(self):
        target = await self.write_note(title="Ratio", body="1:2.5", keys=["ratio"])
        source = await self.write_note(
            title="Hub for coffee",
            body="entry point",
            keys=["coffee"],
            links=[target["note"]["id"]],
            kind="hub",
        )
        plain = await self.read_note(note_id=source["note"]["id"])
        self.assertNotIn("neighbours", plain)

        walked = await self.read_note(note_id=source["note"]["id"], follow_links=True)
        self.assertEqual(walked["neighbours"][0]["id"], target["note"]["id"])
        self.assertEqual(walked["neighbours"][0]["snippet"], "1:2.5")

    async def test_read_note_reports_missing_notes(self):
        self.assertEqual(
            (await self.read_note(note_id="202608190930"))["status"], "not_found"
        )

    async def test_tools_report_disabled_state(self):
        write_note = create_write_note_tool(self.store, is_enabled=False)
        search_note = create_search_note_tool(self.store, is_enabled=False)
        self.assertEqual((await write_note(title="t", body="b"))["status"], "disabled")
        self.assertFalse((await search_note(query=["t"]))["enabled"])


# ----------------------------------------------------------------------
# JournalLLMService: distillation prompt + parsing
# ----------------------------------------------------------------------


class NoteDistillationPromptTests(unittest.IsolatedAsyncioTestCase):
    def test_system_prompt_defends_the_diary_boundary(self):
        prompt = JournalLLMService.build_note_distill_system_prompt(max_notes=2)
        self.assertIn("notebook", prompt)
        self.assertIn("diary already records what happened", prompt)
        self.assertIn("relationship card", prompt)
        self.assertIn("unfinished threads", prompt)
        self.assertIn("zero notes", prompt)
        self.assertIn("At most 2", prompt)
        self.assertIn("first person", prompt)
        self.assertIn("Return JSON only", prompt)

    def test_user_prompt_lists_existing_notes(self):
        prompt = JournalLLMService.build_note_distill_user_prompt(
            existing_notes=[{"title": "Espresso ratio", "tags": ["coffee"]}],
            transcript="[speaker=Jun]: 1:2.5",
        )
        self.assertIn("Espresso ratio", prompt)
        self.assertIn("tags: coffee", prompt)
        self.assertIn("[speaker=Jun]: 1:2.5", prompt)

    def test_user_prompt_handles_an_empty_notebook(self):
        prompt = JournalLLMService.build_note_distill_user_prompt(
            existing_notes=[], transcript="x"
        )
        self.assertIn("(none yet)", prompt)

    def test_parse_note_drafts_accepts_a_list(self):
        drafts = JournalLLMService._parse_note_drafts(
            '[{"title": "A", "body": "b", "keys": ["k"], "tags": ["t"]}]'
        )
        self.assertEqual(drafts, [{"title": "A", "body": "b", "tags": ["t"], "keys": ["k"]}])

    def test_parse_note_drafts_strips_code_fences(self):
        drafts = JournalLLMService._parse_note_drafts(
            '```json\n[{"title": "A", "body": "b"}]\n```'
        )
        self.assertEqual(len(drafts), 1)

    def test_parse_note_drafts_accepts_a_wrapped_object(self):
        drafts = JournalLLMService._parse_note_drafts('{"notes": [{"title": "A", "body": "b"}]}')
        self.assertEqual(len(drafts), 1)

    def test_parse_note_drafts_enforces_the_batch_cap(self):
        payload = '[{"title": "A", "body": "a"}, {"title": "B", "body": "b"}, {"title": "C", "body": "c"}]'
        self.assertEqual(len(JournalLLMService._parse_note_drafts(payload, max_notes=2)), 2)

    def test_parse_note_drafts_skips_incomplete_and_bad_input(self):
        self.assertEqual(JournalLLMService._parse_note_drafts("not json"), [])
        self.assertEqual(JournalLLMService._parse_note_drafts("[]"), [])
        self.assertEqual(
            JournalLLMService._parse_note_drafts('[{"title": "A"}, {"body": "b"}]'), []
        )

    async def test_distill_notes_calls_the_model_and_returns_drafts(self):
        service = JournalLLMService(client=object())
        captured = {}

        async def fake_call_text(system_prompt, user_prompt):
            captured["user"] = user_prompt
            return '[{"title": "Jun likes 1:2.5", "body": "He said thinner has no spine.", "keys": ["espresso"]}]'

        service._call_text = fake_call_text  # type: ignore[assignment]

        drafts = await service.distill_notes(
            messages=[{"role": "user", "sender_id": "jun", "content": "1:2.5 please"}],
            existing_notes=[],
        )
        self.assertEqual(drafts[0]["title"], "Jun likes 1:2.5")
        self.assertIn("1:2.5 please", captured["user"])

    async def test_distill_notes_noops_without_messages(self):
        service = JournalLLMService(client=object())
        self.assertEqual(await service.distill_notes(messages=[], existing_notes=[]), [])


# ----------------------------------------------------------------------
# MemoryHandler: distillation wiring + injection budget
# ----------------------------------------------------------------------


class _FakeDiaryLLMService:
    """Stub journal service that records note-distillation calls."""

    def __init__(self, drafts=None, distill_error=None):
        self.drafts = list(drafts or [])
        self.distill_error = distill_error
        self.distill_calls = []

    async def format_diary_entry(self, messages, journal_date):
        return "\n".join(
            str(message.get("content", "")) for message in messages if message.get("content")
        )

    async def generate_summary(self, source_content, period_type, period_label):
        return ""

    async def update_relationship_cards(self, participants, messages, existing_cards):
        return {}

    async def distill_notes(self, messages, existing_notes, max_notes=2):
        self.distill_calls.append(
            {"messages": messages, "existing_notes": existing_notes, "max_notes": max_notes}
        )
        if self.distill_error is not None:
            raise self.distill_error
        return list(self.drafts)


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

    def _make_handler(self, storage, llm, journal_batch_size=20, notes_auto_distill=True):
        return MemoryHandler(
            memory=self.memory,
            llm_service=llm,
            message_storage=storage,
            journal_batch_size=journal_batch_size,
            note_store=self.notes,
            notes_auto_distill=notes_auto_distill,
        )

    def _batch(self, count=20, sender="jun", channel="feishu"):
        messages = [
            Message.create(content=f"message {index}", role=RoleType.USER, sender_id=sender)
            for index in range(count)
        ]
        for message in messages:
            message.channel = channel
        return messages

    async def test_maintenance_distils_notes_from_the_diary_batch(self):
        storage = _FakeMessageStorage(self._batch())
        llm = _FakeDiaryLLMService(
            drafts=[
                {
                    "title": "Jun takes espresso at 1:2.5",
                    "body": "He said thinner has no spine.",
                    "keys": ["espresso"],
                    "tags": ["coffee"],
                }
            ]
        )
        handler = self._make_handler(storage, llm)

        self.assertTrue(await handler.run_maintenance(force=True))
        self.assertEqual(len(llm.distill_calls), 1)

        notes = await self.notes.list_notes()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].title, "Jun takes espresso at 1:2.5")

    async def test_distilled_note_records_its_provenance(self):
        storage = _FakeMessageStorage(self._batch())
        llm = _FakeDiaryLLMService(
            drafts=[{"title": "Something durable", "body": "worth keeping", "keys": ["thing"]}]
        )
        handler = self._make_handler(storage, llm)
        await handler.run_maintenance(force=True)

        note = (await self.notes.list_notes())[0]
        self.assertTrue(note.source.get("diary"))
        self.assertEqual(note.source.get("person"), "feishu:jun")
        self.assertEqual(note.sensitivity, "person-scoped")
        # The cursor points at the batch the note came from, not the previous
        # checkpoint, so provenance can be resolved back to those messages.
        self.assertEqual(note.source.get("cursor"), 20)

    async def test_distilled_note_is_shareable_when_several_people_took_part(self):
        messages = [*self._batch(count=10, sender="jun"), *self._batch(count=10, sender="mei")]
        storage = _FakeMessageStorage(messages)
        llm = _FakeDiaryLLMService(
            drafts=[{"title": "Group decision", "body": "we agreed on Friday", "keys": ["Friday"]}]
        )
        handler = self._make_handler(storage, llm)
        await handler.run_maintenance(force=True)

        note = (await self.notes.list_notes())[0]
        self.assertEqual(note.sensitivity, "shareable")
        self.assertNotIn("person", note.source)

    async def test_distillation_respects_the_per_batch_cap(self):
        storage = _FakeMessageStorage(self._batch())
        llm = _FakeDiaryLLMService(
            drafts=[
                {"title": f"Note {index}", "body": f"body {index}", "keys": [f"k{index}"]}
                for index in range(5)
            ]
        )
        handler = self._make_handler(storage, llm)
        await handler.run_maintenance(force=True)

        self.assertEqual(
            await self.notes.count(), AgentConfig.NOTES_DISTILL_MAX_PER_BATCH
        )

    async def test_distillation_skips_a_draft_an_existing_note_already_covers(self):
        existing = _note(
            self.notes,
            "Jun takes espresso at 1:2.5",
            "He said thinner has no spine.",
            keys=("espresso", "Jun"),
            tags=("coffee",),
        )
        await self.notes.write(existing)

        storage = _FakeMessageStorage(self._batch())
        llm = _FakeDiaryLLMService(
            drafts=[
                {
                    "title": "Jun espresso 1:2.5",
                    "body": "restating what I already know",
                    "keys": ["espresso", "Jun"],
                    "tags": ["coffee"],
                }
            ]
        )
        handler = self._make_handler(storage, llm)
        await handler.run_maintenance(force=True)

        self.assertEqual(await self.notes.count(), 1)

    async def test_distillation_keeps_a_draft_on_a_different_idea(self):
        existing = _note(
            self.notes,
            "Jun takes espresso at 1:2.5",
            "He said thinner has no spine.",
            keys=("espresso", "Jun"),
            tags=("coffee",),
        )
        await self.notes.write(existing)

        storage = _FakeMessageStorage(self._batch())
        llm = _FakeDiaryLLMService(
            drafts=[
                {
                    "title": "The early train to Hangzhou is the only connection",
                    "body": "Anything later misses the transfer.",
                    "keys": ["train", "Hangzhou"],
                    "tags": ["travel"],
                }
            ]
        )
        handler = self._make_handler(storage, llm)
        await handler.run_maintenance(force=True)

        self.assertEqual(await self.notes.count(), 2)

    async def test_distillation_can_be_switched_off(self):
        storage = _FakeMessageStorage(self._batch())
        llm = _FakeDiaryLLMService(drafts=[{"title": "A", "body": "b"}])
        handler = self._make_handler(storage, llm, notes_auto_distill=False)

        self.assertTrue(await handler.run_maintenance(force=True))
        self.assertEqual(llm.distill_calls, [])
        self.assertEqual(await self.notes.count(), 0)

    async def test_distillation_failure_does_not_break_the_diary_write(self):
        storage = _FakeMessageStorage(self._batch())
        llm = _FakeDiaryLLMService(distill_error=RuntimeError("model exploded"))
        handler = self._make_handler(storage, llm)

        self.assertTrue(await handler.run_maintenance(force=True))
        self.assertEqual(await self.notes.count(), 0)
        recent = await handler.get_recent_context()
        self.assertIn("message 0", recent)

    async def test_notebook_context_is_empty_without_a_store(self):
        handler = MemoryHandler(
            memory=self.memory,
            llm_service=_FakeDiaryLLMService(),
            message_storage=_FakeMessageStorage(),
            journal_batch_size=20,
        )
        self.assertEqual(await handler.get_notebook_context("anything"), "")

    async def test_notebook_context_groups_pinned_hubs_and_recalled_notes(self):
        await self.notes.write(
            _note(self.notes, "Grinder is two clicks coarse", "Dial finer.", keys=("grinder",), pinned=True)
        )
        await self.notes.write(
            _note(self.notes, "Coffee", "entry point", kind=KIND_HUB, keys=("coffee",))
        )
        await self.notes.write(
            _note(self.notes, "Jun 的浓缩固定 1:2.5", "再淡他说没有骨架。", keys=("浓缩",))
        )
        handler = self._make_handler(_FakeMessageStorage(), _FakeDiaryLLMService())

        context = await handler.get_notebook_context("给 Jun 冲个浓缩")
        self.assertIn("[pinned]", context)
        self.assertIn("Grinder is two clicks coarse", context)
        self.assertIn("[hubs]", context)
        self.assertIn("[relevant to the current message]", context)
        self.assertIn("浓缩", context)

    async def test_notebook_context_omits_the_recall_section_without_a_message(self):
        await self.notes.write(
            _note(self.notes, "Jun 的浓缩固定 1:2.5", "body", keys=("浓缩",))
        )
        handler = self._make_handler(_FakeMessageStorage(), _FakeDiaryLLMService())
        self.assertEqual(await handler.get_notebook_context(""), "")

    async def test_notebook_context_does_not_repeat_a_pinned_note(self):
        await self.notes.write(
            _note(self.notes, "Grinder is two clicks coarse", "Dial finer.", keys=("grinder",), pinned=True)
        )
        handler = self._make_handler(_FakeMessageStorage(), _FakeDiaryLLMService())

        context = await handler.get_notebook_context("what about the grinder")
        self.assertEqual(context.count("Grinder is two clicks coarse"), 1)
        self.assertNotIn("[relevant to the current message]", context)

    async def test_notebook_context_marks_non_shareable_notes(self):
        await self.notes.write(
            _note(
                self.notes,
                "Jun keeps the move quiet",
                "He asked me not to mention it.",
                keys=("move",),
                sensitivity="person-scoped",
                pinned=True,
            )
        )
        handler = self._make_handler(_FakeMessageStorage(), _FakeDiaryLLMService())
        context = await handler.get_notebook_context("")
        self.assertIn("[person-scoped]", context)

    async def test_notebook_context_caps_each_section(self):
        for index in range(5):
            await self.notes.write(
                _note(self.notes, f"Pinned {index}", "short", keys=(f"pin{index}",), pinned=True)
            )
        for index in range(7):
            await self.notes.write(
                _note(self.notes, f"Hub {index}", "short", kind=KIND_HUB, keys=(f"hub{index}",))
            )
        for index in range(6):
            await self.notes.write(
                _note(self.notes, f"Loose {index}", "short", keys=(f"loose{index}",))
            )
        handler = self._make_handler(_FakeMessageStorage(), _FakeDiaryLLMService())

        context = await handler.get_notebook_context(
            " ".join(f"loose{index}" for index in range(6))
        )
        self.assertEqual(
            context.count("- ("),
            AgentConfig.NOTEBOOK_PINNED_MAX
            + AgentConfig.NOTEBOOK_HUB_MAX
            + AgentConfig.NOTEBOOK_RELEVANT_MAX,
        )
        for _section, heading in MemoryHandler.NOTEBOOK_SECTIONS:
            self.assertIn(heading, context)

    async def test_notebook_context_stays_within_the_prompt_budget(self):
        for index in range(3):
            await self.notes.write(
                _note(
                    self.notes,
                    f"Pinned {index}",
                    "b" * MAX_BODY_CHARS,
                    keys=(f"pin{index}",),
                    pinned=True,
                )
            )
        for index in range(6):
            await self.notes.write(
                _note(self.notes, f"Hub {index}", "x" * 500, kind=KIND_HUB, keys=(f"hub{index}",))
            )
        for index in range(4):
            await self.notes.write(
                _note(self.notes, f"Loose {index}", "y" * 500, keys=(f"loose{index}",))
            )
        handler = self._make_handler(_FakeMessageStorage(), _FakeDiaryLLMService())

        context = await handler.get_notebook_context(
            " ".join(f"loose{index}" for index in range(4))
        )
        self.assertLessEqual(len(context), AgentConfig.NOTEBOOK_CONTEXT_MAX_CHARS)
        self.assertIn("notes omitted from index due to budget", context)

    async def test_notebook_budget_keeps_pinned_notes_before_navigation(self):
        for index in range(3):
            await self.notes.write(
                _note(
                    self.notes,
                    f"Pinned {index}",
                    "b" * MAX_BODY_CHARS,
                    keys=(f"pin{index}",),
                    pinned=True,
                )
            )
        for index in range(5):
            await self.notes.write(
                _note(self.notes, f"Hub {index}", "short", kind=KIND_HUB, keys=(f"hub{index}",))
            )
        handler = self._make_handler(_FakeMessageStorage(), _FakeDiaryLLMService())

        context = await handler.get_notebook_context("")
        for index in range(3):
            self.assertIn(f"Pinned {index}", context)
        self.assertIn("notes omitted from index due to budget", context)

    async def test_notebook_rows_truncate_long_bodies(self):
        note = _note(self.notes, "Long", "b" * 900, keys=("long",), pinned=True)
        row = MemoryHandler._format_notebook_row(
            note, AgentConfig.NOTEBOOK_PINNED_BODY_MAX_CHARS
        )
        self.assertTrue(row.endswith("..."))
        self.assertLess(len(row), 900)

    def test_notebook_row_for_a_hub_shows_its_link_count(self):
        note = NoteStore.normalize(
            Note(
                id="202608190930",
                title="Coffee",
                body="entry point",
                kind=KIND_HUB,
                links=("202608190931", "202608190932"),
            )
        )
        row = MemoryHandler._format_notebook_row(note, 0)
        self.assertEqual(row, "- (202608190930) Coffee [2 linked]")


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
            notebook_context="[pinned]\n- (202608190930) Grinder is coarse",
        )
        layer = self._layer(messages, AgentConfig.NOTEBOOK_CONTEXT_NAME)
        self.assertIsNotNone(layer)
        self.assertIn("Grinder is coarse", layer["content"])
        self.assertIn("read_note", layer["content"])
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
            notebook_context="[hubs]\n- (202608190930) Coffee",
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
            notebook_context="[pinned]\n- (202608190930) Grinder is coarse",
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
