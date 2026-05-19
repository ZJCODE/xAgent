"""Tests for high-level memory tools."""

import tempfile
import unittest
from pathlib import Path

from xagent.components.memory import ExperienceMemoryStore
from xagent.tools import (
    create_correct_memory_tool,
    create_forget_memory_tool,
    create_recall_memory_tool,
    create_remember_tool,
    create_search_history_tool,
)
from xagent.schemas import Message, RoleType


class MemoryToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.memory = ExperienceMemoryStore(str(Path(self._tmpdir.name) / "xagent_memory.sqlite3"))
        self.enabled = True

    def tearDown(self):
        self._tmpdir.cleanup()

    def _is_enabled(self):
        return self.enabled

    async def test_remember_records_memory_item(self):
        tool = create_remember_tool(self.memory, self._is_enabled)

        result = await tool(
            content="Alice prefers concise implementation plans.",
            kind="preference",
            subject_type="person",
            subject_key="Alice",
        )

        self.assertEqual(result["status"], "ok")
        recalled = await self.memory.recall_memory("concise plans")
        self.assertEqual(recalled["items"][0]["subject"]["key"], "Alice")

    async def test_recall_memory_disabled(self):
        self.enabled = False
        tool = create_recall_memory_tool(self.memory, self._is_enabled)

        result = await tool(query="Alice")

        self.assertEqual(result["status"], "disabled")
        self.assertFalse(result["enabled"])

    async def test_recall_memory_tool(self):
        await self.memory.remember(
            "Alice prefers concise implementation plans.",
            kind="preference",
            subject_type="person",
            subject_key="Alice",
        )
        tool = create_recall_memory_tool(self.memory, self._is_enabled)

        result = await tool(query="concise", include_evidence=True)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["items"][0]["kind"], "preference")

    async def test_search_history_tool(self):
        await self.memory.add_messages(
            Message.create("Older project detail", role=RoleType.USER, sender_id="Alice")
        )
        tool = create_search_history_tool(self.memory, self._is_enabled)

        result = await tool(query="project detail")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["events"][0]["speaker_id"], "Alice")

    async def test_correct_and_forget_tools(self):
        memory_id = await self.memory.remember(
            "The app uses Flask.",
            kind="project_state",
            subject_type="project",
            subject_key="xAgent",
        )
        correct = create_correct_memory_tool(self.memory, self._is_enabled)
        forget = create_forget_memory_tool(self.memory, self._is_enabled)

        corrected = await correct(memory_id=memory_id, correction="The app uses FastAPI.", reason="user corrected it")
        forgotten = await forget(memory_id=memory_id, mode="archive", reason="user asked")

        self.assertEqual(corrected["status"], "ok")
        self.assertEqual(forgotten["status"], "ok")
        self.assertEqual((await self.memory.recall_memory("FastAPI"))["items"], [])


if __name__ == "__main__":
    unittest.main()
