"""Tests for the unified experience memory store."""

import tempfile
import unittest
from pathlib import Path

from xagent.components.memory import ExperienceMemoryStore
from xagent.schemas import Message, RoleType


class ExperienceMemoryStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "xagent_memory.sqlite3"
        self.store = ExperienceMemoryStore(str(self.db_path))

    def tearDown(self):
        self._tmpdir.cleanup()

    async def test_message_roundtrip_uses_events_table(self):
        message = Message.create("project detail", role=RoleType.USER, sender_id="alice")
        await self.store.add_messages(message)

        self.assertIn("event_id", message.metadata)
        messages = await self.store.get_messages(10)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].content, "project detail")
        self.assertEqual(messages[0].sender_id, "alice")

    async def test_remember_and_recall_with_evidence(self):
        message = Message.create("Alice prefers concise plans", role=RoleType.USER, sender_id="Alice")
        await self.store.add_messages(message)

        memory_id = await self.store.remember(
            "Alice prefers concise plans",
            kind="preference",
            subject_type="person",
            subject_key="Alice",
            evidence_event_ids=[message.metadata["event_id"]],
            evidence_note="prefers concise plans",
        )
        result = await self.store.recall_memory("concise plans", include_evidence=True)

        self.assertEqual(result["items"][0]["memory_id"], memory_id)
        self.assertEqual(result["items"][0]["kind"], "preference")
        self.assertEqual(result["items"][0]["evidence"][0]["event_id"], message.metadata["event_id"])

    async def test_search_history_uses_raw_events(self):
        await self.store.add_messages(
            Message.create("Older exact wording", role=RoleType.USER, sender_id="alice")
        )

        result = await self.store.search_history("exact wording")

        self.assertEqual(result["events"][0]["content"], "Older exact wording")

    async def test_correct_and_forget_memory(self):
        memory_id = await self.store.remember(
            "The project uses Flask.",
            kind="project_state",
            subject_type="project",
            subject_key="xAgent",
        )

        await self.store.correct_memory(
            memory_id=memory_id,
            correction="The project uses FastAPI.",
            reason="user correction",
        )
        corrected = await self.store.get_memory_item(memory_id)
        self.assertEqual(corrected["content"], "The project uses FastAPI.")

        await self.store.forget_memory(memory_id=memory_id, mode="archive")
        recalled = await self.store.recall_memory("FastAPI", max_items=5)
        self.assertEqual(recalled["items"], [])

    async def test_readonly_debug_query_rejects_writes(self):
        with self.assertRaisesRegex(ValueError, "Only SELECT or WITH"):
            await self.store.query_sql("DELETE FROM memory_items")


if __name__ == "__main__":
    unittest.main()
