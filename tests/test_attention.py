import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from xagent.core.attention import AttentionLoop, make_room_key
from xagent.components.message import MessageStorage
from xagent.schemas import Message, ParticipationDecision, RoleType


class AttentionLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_five_notices_become_one_decision(self):
        decisions = []
        spoken = []

        async def decide(**kwargs):
            decisions.append(kwargs)
            return ParticipationDecision(should_reply=True, reason="join")

        async def speak(room_key, through_cursor, **kwargs):
            spoken.append((room_key, through_cursor, kwargs["events"]))

        loop = AttentionLoop(
            quiet_window=0.05,
            max_wait=1,
            decide=decide,
        )
        loop.register_speaker("feishu", speak)
        room = make_room_key("feishu", "oc_group")
        for index in range(5):
            await loop.notice(
                room,
                addressed=False,
                content=f"line {index}",
                cursor=index + 1,
                sender_id="ou_alice",
                sender_name="Alice",
            )
        await loop.idle()
        self.assertEqual(len(decisions), 1)
        self.assertIn("line 0", decisions[0]["context"])
        self.assertIn("line 4", decisions[0]["context"])
        self.assertEqual(len(spoken), 1)
        self.assertEqual(len(spoken[0][2]), 5)
        self.assertEqual(loop.cursors(room).attended_through, spoken[0][1])
        await loop.stop()

    async def test_addressed_messages_skip_decision_and_must_speak(self):
        decisions = []
        spoken = []

        async def decide(**kwargs):
            decisions.append(kwargs)
            return ParticipationDecision(should_reply=False, reason="should not run")

        async def speak(room_key, through_cursor, **kwargs):
            spoken.append(kwargs["events"])

        loop = AttentionLoop(
            quiet_window=5,
            max_wait=5,
            decide=decide,
        )
        loop.register_speaker("feishu", speak)
        await loop.notice(
            "feishu:oc_dm",
            addressed=True,
            content="hello",
            cursor=1,
            sender_id="ou_user",
        )
        await loop.idle()
        self.assertEqual(decisions, [])
        self.assertEqual(len(spoken), 1)
        await loop.stop()

    async def test_failed_speak_does_not_advance_cursor(self):
        async def decide(**kwargs):
            return ParticipationDecision(should_reply=True, reason="speak")

        async def speak(room_key, through_cursor, **kwargs):
            raise RuntimeError("delivery failed")

        loop = AttentionLoop(
            quiet_window=0,
            max_wait=0,
            decide=decide,
        )
        loop.register_speaker("feishu", speak)
        await loop.notice("feishu:oc_group", addressed=False, content="ping", cursor=1)
        with self.assertRaises(asyncio.TimeoutError):
            await loop.idle(timeout=0.2)
        self.assertEqual(loop.cursors("feishu:oc_group").attended_through, 0)
        self.assertEqual(len(loop.cursors("feishu:oc_group").events), 1)
        await loop.stop()

    async def test_reset_to_present_skips_backlog(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = MessageStorage(path=str(Path(tmpdir) / "messages.sqlite3"))
            first = Message.create("old", role=RoleType.USER, sender_id="alice")
            first.metadata["room_key"] = "feishu:hall"
            stored = await storage.add_messages(first)
            loop = AttentionLoop(
                store_path=Path(tmpdir) / ".attention.json",
                quiet_window=0,
                max_wait=0,
            )
            await loop.reset_to_present(storage)
            self.assertEqual(
                loop.cursors("feishu:hall").attended_through,
                stored[0].metadata["storage_cursor"],
            )
            await loop.stop()


class AttentionHelperTests(unittest.TestCase):
    def test_make_room_key(self):
        self.assertEqual(make_room_key("feishu", "oc_1"), "feishu:oc_1")
