import tempfile
import unittest
from pathlib import Path

from xagent.components.message import MessageStorage
from xagent.schemas import Message, MessageType, RoleType


class MessageStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_cursor_range_is_stable_when_newer_messages_arrive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "messages.sqlite3"
            storage = MessageStorage(path=str(db_path))
            await storage.add_messages([
                Message.create("first", role=RoleType.USER, sender_id="alice"),
                Message.create("second", role=RoleType.USER, sender_id="alice"),
                Message.create("third", role=RoleType.USER, sender_id="alice"),
            ])

            first_cursor = await storage.cursor_for_message_count(1)
            snapshot_cursor = await storage.get_latest_message_cursor()

            await storage.add_messages([
                Message.create("newer one", role=RoleType.USER, sender_id="alice"),
                Message.create("newer two", role=RoleType.USER, sender_id="alice"),
            ])

            messages = await storage.get_messages_in_cursor_range(
                start_exclusive=first_cursor,
                end_inclusive=snapshot_cursor,
            )

            self.assertEqual([message.content for message in messages], ["second", "third"])

    async def test_cursor_for_message_count_returns_zero_when_stream_shrinks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "messages.sqlite3"
            storage = MessageStorage(path=str(db_path))
            await storage.add_messages([
                Message.create("first", role=RoleType.USER, sender_id="alice"),
                Message.create("second", role=RoleType.USER, sender_id="alice"),
            ])

            self.assertGreater(await storage.cursor_for_message_count(2), 0)
            self.assertEqual(await storage.cursor_for_message_count(3), 0)

    async def test_clear_messages_resets_stream(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "messages.sqlite3"
            storage = MessageStorage(path=str(db_path))
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
            storage = MessageStorage(path=str(db_path))
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
