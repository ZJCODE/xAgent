import tempfile
import unittest
from pathlib import Path

from xagent.components.message import MessageStorageLocal
from xagent.schemas import Message, MessageType, RoleType


class MessageStorageLocalTests(unittest.IsolatedAsyncioTestCase):
    async def test_clear_messages_resets_stream(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "messages.sqlite3"
            storage = MessageStorageLocal(path=str(db_path))
            await storage.add_messages([
                Message.create("first", role=RoleType.USER, sender_id="alice"),
                Message.create("second", role=RoleType.USER, sender_id="bob"),
            ])

            self.assertEqual(await storage.get_message_count(), 2)
            await storage.clear_messages()
            self.assertEqual(await storage.get_message_count(), 0)

    async def test_context_event_roundtrips_with_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "messages.sqlite3"
            storage = MessageStorageLocal(path=str(db_path))
            event = Message.create_context_event(
                "看到有人靠近",
                source="camera",
                event_type="presence",
                metadata={"memory_policy": "always"},
            )

            await storage.add_messages(event)
            messages = await storage.get_messages(10)

            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0].type, MessageType.CONTEXT_EVENT)
            self.assertEqual(messages[0].role, RoleType.ENVIRONMENT)
            self.assertIsNone(messages[0].sender_id)
            self.assertEqual(messages[0].metadata["source"], "camera")
            self.assertEqual(messages[0].metadata["event_type"], "presence")
            self.assertEqual(messages[0].metadata["memory_policy"], "always")

    async def test_structured_columns_are_queryable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "messages.sqlite3"
            storage = MessageStorageLocal(path=str(db_path))
            await storage.add_messages(
                Message.create("project detail", role=RoleType.USER, sender_id="alice")
            )

            result = await storage.query_sql(
                "SELECT role, type, sender_id, content FROM messages WHERE content LIKE '%project%'"
            )

            self.assertEqual(result["rows"][0]["role"], "user")
            self.assertEqual(result["rows"][0]["type"], "message")
            self.assertEqual(result["rows"][0]["sender_id"], "alice")

    async def test_readonly_query_rejects_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "messages.sqlite3"
            storage = MessageStorageLocal(path=str(db_path))

            with self.assertRaisesRegex(ValueError, "Only SELECT or WITH"):
                await storage.query_sql("DELETE FROM messages")
