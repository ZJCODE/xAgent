import sqlite3
import tempfile
import unittest
from pathlib import Path

from xagent.components.message.local_messages import MessageStorageLocal
from xagent.schemas import Message, RoleType


class MessageStorageLocalTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_schema_is_migrated_in_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "messages.sqlite3"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        conversation_id TEXT NOT NULL,
                        timestamp REAL NOT NULL,
                        message_json TEXT NOT NULL
                    )
                    """
                )
                rows = [
                    ("alpha", 1.0, Message.create("first", role=RoleType.USER, sender_id="alice").model_dump_json()),
                    ("beta", 2.0, Message.create("second", role=RoleType.ASSISTANT, sender_id="agent:test").model_dump_json()),
                    ("alpha", 3.0, Message.create("third", role=RoleType.USER, sender_id="bob").model_dump_json()),
                ]
                conn.executemany(
                    """
                    INSERT INTO messages (conversation_id, timestamp, message_json)
                    VALUES (?, ?, ?)
                    """,
                    rows,
                )
                conn.commit()

            storage = MessageStorageLocal(path=str(db_path))
            messages = await storage.get_messages(10)
            self.assertEqual([message.content for message in messages], ["first", "second", "third"])
            self.assertEqual(await storage.get_message_count(), 3)

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
