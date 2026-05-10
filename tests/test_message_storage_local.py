import tempfile
import unittest
from pathlib import Path

from xagent.components.message import MessageStorageLocal
from xagent.schemas import Message, RoleType


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
