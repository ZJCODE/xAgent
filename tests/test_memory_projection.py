"""Committed diary evidence, projection boundaries, and restart-safe writes."""

import asyncio
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

from xagent.components.memory import MarkdownMemory, Note
from xagent.components.message import MessageStorage
from xagent.core.handlers.memory import MemoryHandler
from xagent.core.journal import JournalLLMService
from xagent.schemas import Message, RoleType


class ProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.memory = MarkdownMemory(str(self.root / "memory"))
        self.storage = MessageStorage(str(self.root / "messages.sqlite3"))
        self.llm = AsyncMock()
        self.llm.format_diary_entry.return_value = "Alice asked me to lead with conclusions. For the offline project, we chose SQLite because it needs no server."
        self.llm.update_memory_projections.return_value = {}
        self.handler = self.make_handler()

    def tearDown(self):
        self.directory.cleanup()

    def make_handler(self):
        return MemoryHandler(memory=self.memory, llm_service=self.llm,
                             message_storage=self.storage, diary_write_batch=20)

    async def message(self, content="Discuss the offline project.", sender="alice", name="Alice", room=None):
        await self.storage.add_messages(Message(
            content=content, role=RoleType.USER, sender_id=sender, channel="api",
            metadata={"sender_name": name}, room_name=room,
        ))

    @staticmethod
    def note(title="Offline storage", body="Use SQLite for this offline project because no server is available.", **kwargs):
        return {"title": title, "body": body, "keys": ["offline", "SQLite"], "links": [], **kwargs}

    async def test_one_call_after_commit_separates_person_from_topic_and_assigns_sources(self):
        await self.message()

        async def project(**kwargs):
            self.assertIn(kwargs["committed_diary"], self.memory.daily_path(date.today()).read_text())
            self.assertEqual(self.handler._read_state_sync(), 1)
            self.assertEqual(kwargs["participants"][0]["key"], "api:alice")
            return {"relationships": {"api:alice": "Alice prefers that I lead with conclusions."},
                    "notes": [self.note()]}

        self.llm.update_memory_projections.side_effect = project
        self.assertTrue(await self.handler.run_maintenance(force=True))
        self.llm.update_memory_projections.assert_awaited_once()
        card = await self.handler.relationship_store.read_card("api:alice")
        notes = await self.handler.note_store.list_notes()
        self.assertEqual(card.source, {"diary": [date.today().isoformat()]})
        self.assertEqual(notes[0].source, card.source)
        self.assertNotIn("Alice", notes[0].body)
        self.assertNotIn("created", notes[0].to_dict())
        self.assertNotIn("source_diary_dates", self.handler.relationship_store.card_path(card.key).read_text())

    async def test_no_change_experiences_create_no_long_term_records(self):
        for diary in ("Alice sent a greeting.", "Alice tested with 123.",
                      "The display was temporarily offline.", "I gave an ordinary explanation of HTTP."):
            self.llm.format_diary_entry.return_value = diary
            await self.message(diary)
            self.assertTrue(await self.handler.run_maintenance(force=True))
        self.assertEqual(await self.handler.note_store.count(), 0)
        self.assertEqual(await self.handler.relationship_store.list_keys(), [])
        self.assertEqual(self.llm.update_memory_projections.await_count, 4)

    async def test_identical_confirmation_preserves_ids_dates_and_bytes(self):
        await self.message()
        self.llm.update_memory_projections.return_value = {
            "relationships": {"api:alice": "Alice prefers conclusions first."}, "notes": [self.note()],
        }
        await self.handler.run_maintenance(force=True)
        note = (await self.handler.note_store.list_notes())[0]
        note_path = self.handler.note_store.path_for(note.id)
        card_path = self.handler.relationship_store.card_path("api:alice")
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in (note_path, card_path)]
        self.llm.update_memory_projections.return_value["notes"][0]["id"] = note.id
        self.llm.update_memory_projections.return_value["notes"][0]["keys"] = ["SQLite", "OFFLINE"]
        await self.message("I confirm the same decision.")
        await self.handler.run_maintenance(force=True)
        self.assertEqual([(p.read_bytes(), p.stat().st_mtime_ns) for p in (note_path, card_path)], before)
        self.assertEqual(await self.handler.note_store.count(), 1)

    async def test_topic_candidates_keep_different_conclusions_and_include_one_hop(self):
        original = await self.handler.note_store.create(Note(
            id="", title="Offline storage", body="SQLite needs no server.", keys=("SQLite",)))
        application = await self.handler.note_store.create(Note(
            id="", title="Local installation", body="Uses offline storage.", links=(original.id,)))
        self.llm.update_memory_projections.return_value = {"notes": [
            self.note("Offline backups", "SQLite needs regular local backups.", links=[original.id]),
        ]}
        await self.message()
        await self.handler.run_maintenance(force=True)
        self.assertEqual(await self.handler.note_store.count(), 3)
        inputs = self.llm.update_memory_projections.call_args.kwargs
        self.assertEqual(inputs["existing_notes"][0]["id"], original.id)
        self.assertEqual(inputs["related_notes"][0]["id"], application.id)
        self.assertEqual(len(await self.handler.note_store.backlinks(original.id)), 2)

    async def test_source_day_is_diary_day_and_corrections_keep_id_and_history(self):
        await self.message()
        self.llm.update_memory_projections.return_value = {"notes": [self.note()]}
        with patch("xagent.core.handlers.memory.date") as clock:
            clock.today.return_value = date(2026, 10, 1)
            await self.handler.run_maintenance(force=True)
        original = (await self.handler.note_store.list_notes())[0]
        self.llm.update_memory_projections.return_value = {"notes": [
            self.note(body="SQLite is still appropriate; use WAL for concurrent readers.", id=original.id)]}
        await self.message("A new SQLite constraint.")
        with patch("xagent.core.handlers.memory.date") as clock:
            clock.today.return_value = date(2026, 10, 2)
            await self.handler.run_maintenance(force=True)
        current = await self.handler.note_store.read(original.id)
        self.assertIn("WAL", current.body)
        self.assertEqual(current.updated, "2026-10-02")
        self.assertEqual(current.source, {"diary": ["2026-10-01", "2026-10-02"]})

    async def test_one_hop_summary_is_not_permission_to_overwrite_that_note(self):
        base = await self.handler.note_store.create(Note(
            id="", title="Offline storage", body="Use SQLite.", keys=("SQLite",)))
        neighbour = await self.handler.note_store.create(Note(
            id="", title="Installation", body="Preserve this complete explanation.", links=(base.id,)))
        await self.message()
        self.llm.update_memory_projections.return_value = {
            "notes": [self.note(id=neighbour.id, body="An uninformed rewrite.")]}
        await self.handler.run_maintenance(force=True)
        self.assertIn("projection_error", self.handler._last_maintenance)
        self.assertEqual((await self.handler.note_store.read(neighbour.id)).body, neighbour.body)

    async def test_source_budget_preserves_whole_entries_and_utf8_offsets(self):
        first, second = "第一段经历。" * 30, "第二段经历。" * 30
        await self.handler.append_daily_entry(first)
        await self.handler.append_daily_entry(second)
        self.handler.projector.source_chars = 80
        await self.handler.run_maintenance(force=True)
        await self.handler.run_maintenance(force=True)
        await self.handler.run_maintenance(force=True)
        sources = [call.kwargs["committed_diary"] for call in self.llm.update_memory_projections.call_args_list]
        self.assertEqual(len(sources), 2)
        self.assertIn(first, sources[0])
        self.assertNotIn(second, sources[0])
        self.assertIn(second, sources[1])
        self.assertNotIn(first, sources[1])

    async def test_result_must_be_saved_before_any_projection_file_changes(self):
        await self.message()
        self.llm.update_memory_projections.return_value = {"notes": [self.note()]}
        save = self.handler.projector._save

        async def fail_result(state):
            if state["pending"]:
                raise OSError("cannot save generated result")
            await save(state)

        with patch.object(self.handler.projector, "_save", side_effect=fail_result):
            self.assertTrue(await self.handler.run_maintenance(force=True))
        self.assertEqual(await self.handler.note_store.count(), 0)
        self.assertIn("projection_error", self.handler._last_maintenance)
        await self.handler.run_maintenance(force=True)
        self.assertEqual(await self.handler.note_store.count(), 1)

    async def test_generation_failure_does_not_block_new_diary_and_is_retried(self):
        await self.message()
        self.llm.update_memory_projections.side_effect = RuntimeError("model unavailable")
        self.assertTrue(await self.handler.run_maintenance(force=True))
        await self.message("A second experience.")
        self.assertTrue(await self.handler.run_maintenance(force=True))
        before = self.memory.daily_path(date.today()).read_bytes()
        self.llm.update_memory_projections.side_effect = None
        self.llm.update_memory_projections.return_value = {"notes": [self.note()]}
        self.handler = self.make_handler()
        self.assertFalse(await self.handler.run_maintenance(force=True))
        self.assertEqual(await self.handler.note_store.count(), 1)
        self.assertEqual(self.memory.daily_path(date.today()).read_bytes(), before)
        self.assertEqual(self.llm.format_diary_entry.await_count, 2)
        self.assertEqual(self.llm.update_memory_projections.await_count, 3)

    async def test_partial_writes_resume_same_ids_without_regeneration(self):
        await self.message()
        self.llm.update_memory_projections.return_value = {"notes": [
            self.note(), self.note("Offline backups", "Keep local backups of SQLite.")]}
        write = self.handler.note_store.write
        calls = 0

        async def fail_second(note):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("disk interrupted")
            return await write(note)

        with patch.object(self.handler.note_store, "write", side_effect=fail_second):
            self.assertTrue(await self.handler.run_maintenance(force=True))
        state = json.loads(self.handler.projector.path.read_text())
        assigned = {row["id"] for row in state["pending"]["notes"]}
        self.assertEqual(await self.handler.note_store.count(), 1)
        self.handler = self.make_handler()
        await self.handler.run_maintenance(force=True)
        self.assertEqual({n.id for n in await self.handler.note_store.list_notes()}, assigned)
        self.llm.update_memory_projections.assert_awaited_once()
        self.llm.format_diary_entry.assert_awaited_once()
        self.assertIsNone(json.loads(self.handler.projector.path.read_text())["pending"])

    async def test_cancelled_application_drains_writes_then_restart_does_not_regenerate(self):
        await self.message()
        self.llm.update_memory_projections.return_value = {"notes": [self.note()]}
        started, release = asyncio.Event(), asyncio.Event()
        write = self.handler.note_store.write

        async def delayed(note):
            started.set()
            await release.wait()
            return await write(note)

        with patch.object(self.handler.note_store, "write", side_effect=delayed):
            task = asyncio.create_task(self.handler.run_maintenance(force=True))
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.handler = self.make_handler()
        await self.handler.run_maintenance(force=True)
        self.assertEqual(await self.handler.note_store.count(), 1)
        self.llm.update_memory_projections.assert_awaited_once()

    async def test_uncommitted_diary_bytes_are_never_projected(self):
        await self.message()
        with patch.object(self.handler, "_commit_processed_message_id", AsyncMock(return_value=False)):
            self.assertFalse(await self.handler.run_maintenance(force=True))
        self.llm.update_memory_projections.assert_not_awaited()
        await self.handler.recover_pending_commit()
        await self.handler.run_maintenance(force=True)
        self.llm.update_memory_projections.assert_awaited_once()

    async def test_ambiguous_names_do_not_merge_people_in_group_or_private_context(self):
        await self.message(sender="first_id", name="Alex", room="Team")
        await self.message(sender="second_id", name="Alex")
        self.llm.format_diary_entry.return_value = "Alex prefers concise answers."
        await self.handler.run_maintenance(force=True)
        self.assertEqual(self.llm.update_memory_projections.call_args.kwargs["participants"], [])
        self.assertEqual(await self.handler.relationship_store.list_keys(), [])

    async def test_rename_keeps_identity_for_backlogged_diary(self):
        await self.message(sender="person_id", name="Alice")
        self.llm.update_memory_projections.side_effect = RuntimeError("offline")
        await self.handler.run_maintenance(force=True)
        await self.message(sender="person_id", name="Alicia")
        self.llm.update_memory_projections.side_effect = None
        self.llm.update_memory_projections.return_value = {"relationships": {"api:person_id": "Alice prefers conclusions first."}}
        await self.handler.run_maintenance(force=True)
        card = await self.handler.relationship_store.read_card("api:person_id")
        self.assertEqual(card.display_name, "Alicia")

    async def test_invalid_model_data_cannot_write_unknown_people_or_records(self):
        await self.message()
        for changes in (
            {"relationships": {"api:other": "Invented."}},
            {"relationships": {"api:alice": {"body": "Not a string"}}},
            {"notes": [self.note(id="202601010001")]},
            {"notes": [self.note(body="x" * 2001)]},
            {"notes": [{"body": None}]},
        ):
            with self.subTest(changes=changes):
                self.llm.update_memory_projections.return_value = changes
                await self.handler.run_maintenance(force=True)
                self.assertEqual(await self.handler.note_store.count(), 0)
                self.assertEqual(await self.handler.relationship_store.list_keys(), [])
                self.assertIn("projection_error", self.handler._last_maintenance)

    async def test_removals_and_invalid_references_do_not_resurrect_records(self):
        await self.message()
        target = await self.handler.note_store.create(Note(
            id="", title="SQLite", body="Old constraint.", keys=("SQLite",)))
        self.llm.update_memory_projections.return_value = {"notes": [
            {"id": target.id, "body": None},
            self.note(body=f"New conclusion; old reference [[{target.id}]] is obsolete.",
                      links=[target.id, "999999999999"]),
        ]}
        await self.handler.run_maintenance(force=True)
        self.assertIsNone(await self.handler.note_store.read(target.id))
        current = (await self.handler.note_store.list_notes())[0]
        self.assertEqual(current.links, ())
        self.assertNotIn("[[", current.body)


class ProjectionModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_json_and_types_are_errors_not_no_change(self):
        service = JournalLLMService(client=object())
        for output in ("not json", "[]", '{"notes": {}}', '{"relationships": []}', '{"unexpected": 1}'):
            with self.subTest(output=output):
                service._call_text = AsyncMock(return_value=output)
                with self.assertRaises((ValueError, TypeError)):
                    await service.update_memory_projections(committed_diary="A real experience.",
                        participants=[], existing_cards={}, existing_notes=[], related_notes=[])

    async def test_empty_diary_never_calls_model_and_empty_result_is_valid(self):
        service = JournalLLMService(client=object())
        service._call_text = AsyncMock(return_value="{}")
        await service.update_memory_projections(committed_diary="", participants=[],
            existing_cards={}, existing_notes=[], related_notes=[])
        service._call_text.assert_not_awaited()
        result = await service.update_memory_projections(committed_diary="I greeted Alice.",
            participants=[], existing_cards={}, existing_notes=[], related_notes=[])
        self.assertEqual(result, {"relationships": {}, "notes": []})
