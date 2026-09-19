"""xAgent world inhabitant: a mind enters the world as its own body."""

from __future__ import annotations

import asyncio
import base64
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from agents_world.client import WorldClient
from agents_world.server import WorldHub
from xagent.core.formatters import RoomSnapshot
from xagent.core.inbox import InboxKind
from xagent.core.runtime import current_delivery_context, scheduled_delivery_context
from xagent.integrations.world import WorldInhabitant
from xagent.integrations.world.presence import mark_world_presence, read_world_presence
from xagent.interfaces.server import AgentHTTPServer
from xagent.tools.scheduler_tool import create_schedule_task_tool


class _FakeStorage:
    async def clear_messages(self):
        return None


class _FakeMessageHandler:
    def __init__(self):
        self.users = []

    async def store_user_message(self, user_message, user_id="", **kwargs):
        self.users.append({"user_message": user_message, "user_id": user_id, **kwargs})


class StubAgent:
    model = "test-model"
    tools = {}
    message_storage = None
    workspace = None

    def __init__(self, workspace_dir=None):
        self.observed = []
        self.chats = []
        self.decisions = []
        self.should_reply = True
        self.message_storage = _FakeStorage()
        self.message_handler = _FakeMessageHandler()
        if workspace_dir is not None:
            self.workspace_dir = Path(workspace_dir)

    async def observe(self, **kwargs):
        self.observed.append(kwargs)

    async def decide_participation(self, **kwargs):
        self.decisions.append(kwargs)
        return type("Decision", (), {"should_reply": self.should_reply, "reason": "test"})()

    async def chat(self, user_message, user_id="", **kwargs):
        self.chats.append({"user_message": user_message, "user_id": user_id, **kwargs})
        return "I heard that"


def _room_context_text(value) -> str:
    if isinstance(value, RoomSnapshot):
        return value.render()
    return str(value or "")


class WorldInhabitantTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._pause_patch = patch.object(
            WorldInhabitant,
            "_listening_pause_seconds",
            return_value=0.0,
        )
        self._pause_patch.start()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.hub = WorldHub.create(
            host="127.0.0.1",
            port=0,
            data_root=self._tmpdir.name,
        )
        self.hub.create_world(world_id="mind-venue", name="大厅")
        self.port = await self.hub.start()
        self.url = f"ws://127.0.0.1:{self.port}/ws/mind-venue"
        self.workspace = Path(self._tmpdir.name) / "agent-ws"
        self.workspace.mkdir()
        self.agent = StubAgent(workspace_dir=self.workspace)

    async def asyncTearDown(self):
        self._pause_patch.stop()
        await self.hub.stop()
        self._tmpdir.cleanup()

    async def _wait_present(self, member_id="agent1"):
        world = self.hub.get("mind-venue")
        for _ in range(50):
            session = world._sessions.get(member_id) if world else None
            if session is not None and session.present:
                return
            await asyncio.sleep(0.05)

    def test_participation_context_highlights_trigger_line(self):
        inhabitant = WorldInhabitant(self.agent, member_id="agent1", display_name="一号")
        inhabitant._names["alice"] = "爱丽丝"
        event = {"kind": "utterance", "actor_id": "alice", "seq": 5, "text": "有人吗"}
        inhabitant._recent = [event]
        ctx = inhabitant._participation_decision_context(event)
        self.assertIn("爱丽丝: 有人吗", ctx)
        self.assertIn("beat is still open", ctx)
        self.assertNotIn("Named you:", ctx)
        event_named = {**event, "text": "@agent1 有人吗"}
        ctx_named = inhabitant._participation_decision_context(event_named)
        self.assertIn("@ you or used your name", ctx_named)

    def test_replies_after_trigger_lists_peer_lines(self):
        inhabitant = WorldInhabitant(self.agent, member_id="agent2", display_name="二号")
        inhabitant._names["agent1"] = "一号"
        trigger = {"kind": "utterance", "actor_id": "human", "seq": 10, "text": "hi"}
        inhabitant._recent = [
            trigger,
            {"kind": "utterance", "actor_id": "agent1", "seq": 11, "text": "hello back"},
        ]
        replies = inhabitant._replies_after_trigger(trigger)
        self.assertEqual(replies, [("一号", "hello back")])
        formatted = inhabitant._format_peer_replies(replies)
        self.assertIn("一号", formatted)
        self.assertIn("hello back", formatted)

    async def test_listening_pause_stops_when_peer_answered(self):
        inhabitant = WorldInhabitant(self.agent, member_id="agent1", display_name="一号")
        trigger = {"kind": "utterance", "actor_id": "human", "seq": 1, "text": "anyone?"}
        inhabitant._recent = [trigger]
        inhabitant._names["agent2"] = "二号"

        async def add_peer_line() -> None:
            await asyncio.sleep(0.08)
            inhabitant._recent.append(
                {"kind": "utterance", "actor_id": "agent2", "seq": 2, "text": "here"},
            )

        with patch.object(inhabitant, "_listening_pause_seconds", return_value=3.0):
            started = asyncio.get_running_loop().time()
            peer = asyncio.create_task(add_peer_line())
            await inhabitant._listening_pause(trigger)
            await peer
            elapsed = asyncio.get_running_loop().time() - started
        self.assertLess(elapsed, 1.0)

    async def test_hears_utterance_and_speaks(self):
        inhabitant = WorldInhabitant(self.agent, member_id="agent1", display_name="一号")
        await inhabitant.join(world_url=self.url)
        try:
            await self._wait_present()
            async with WorldClient(self.url, member_id="alice") as alice:
                await alice.join()
                await alice.wait_for(lambda m: m.get("type") == "snapshot")
                await alice.speak( "hello hall")
                heard = await alice.wait_for(
                    lambda m: m.get("type") == "event"
                    and m.get("kind") == "utterance"
                    and m.get("actor_id") == "agent1",
                    timeout=8.0,
                )
                self.assertEqual(heard.get("text"), "I heard that")
        finally:
            await inhabitant.leave()
        self.assertTrue(self.agent.observed)
        self.assertEqual(self.agent.chats[0]["user_message"], "hello hall")
        self.assertEqual(self.agent.chats[0]["user_id"], "alice")
        self.assertEqual(self.agent.chats[0]["sender_name"], "alice")
        self.assertEqual(self.agent.chats[0].get("inbox_kind"), InboxKind.PRESENCE_TURN)
        self.assertEqual(self.agent.message_handler.users, [])
        self.assertFalse(self.agent.chats[0].get("channel_instructions"))
        self.assertEqual(self.agent.decisions[0]["metadata"]["addressed_to_agent"], False)
        self.assertEqual(self.agent.decisions[0]["metadata"]["recently_spoke"], False)
        self.assertTrue(any("hello hall" in str(item.get("context") or "") for item in self.agent.observed))

    async def test_decide_and_speak_share_room_situation(self):
        inhabitant = WorldInhabitant(self.agent, member_id="agent1", display_name="一号")
        async with WorldClient(self.url, member_id="alice", display_name="爱丽丝") as alice:
            await alice.join()
            await alice.wait_for(lambda m: m.get("type") == "snapshot")
            await inhabitant.join(world_url=self.url)
            try:
                await self._wait_present()
                await alice.speak("有人吗")
                heard = await alice.wait_for(
                    lambda m: m.get("type") == "event"
                    and m.get("kind") == "utterance"
                    and m.get("actor_id") == "agent1",
                    timeout=8.0,
                )
                self.assertEqual(heard.get("text"), "I heard that")
            finally:
                await inhabitant.leave()

        decision_context = str(self.agent.decisions[0].get("context") or "")
        self.assertIn("有人吗", decision_context)
        chat = self.agent.chats[0]
        room_block = _room_context_text(chat.get("room_context"))
        self.assertEqual(chat["user_message"], "有人吗")
        self.assertEqual(chat.get("inbox_kind"), InboxKind.PRESENCE_TURN)
        self.assertEqual(self.agent.message_handler.users, [])
        self.assertNotIn("[room context]", chat["user_message"])
        self.assertIn("[room context]", decision_context)
        self.assertIn("[room context]", room_block)
        self.assertIn("present:", room_block)
        self.assertIn("一号", room_block)
        self.assertIn("爱丽丝", room_block)
        self.assertNotIn("一号(agent1)", room_block)
        self.assertNotIn("爱丽丝(alice)", room_block)
        self.assertNotIn("(player2)", room_block)
        self.assertIn("有人吗", room_block)
        self.assertRegex(room_block, r"爱丽丝 \d{4}-\d{2}-\d{2} \d{2}:\d{2}: 有人吗")
        # Same situation object for decide and speak (block text matches).
        self.assertIn(room_block, decision_context)
        self.assertTrue(any("有人吗" in str(item.get("context") or "") for item in self.agent.observed))

    async def test_silence_stores_trigger_not_room_block(self):
        self.agent.should_reply = False
        inhabitant = WorldInhabitant(self.agent, member_id="agent1", display_name="一号")
        await inhabitant.join(world_url=self.url)
        try:
            await self._wait_present()
            async with WorldClient(self.url, member_id="alice", display_name="爱丽丝") as alice:
                await alice.join()
                await alice.wait_for(lambda m: m.get("type") == "snapshot")
                await alice.speak("the coffee is hot")
                for _ in range(40):
                    if any("the coffee is hot" in str(item.get("context") or "") for item in self.agent.observed):
                        break
                    await asyncio.sleep(0.05)
        finally:
            await inhabitant.leave()

        heard = next(
            item for item in self.agent.observed if "the coffee is hot" in str(item.get("context") or "")
        )
        self.assertEqual(heard["context"], "the coffee is hot")
        self.assertNotIn("[room context]", heard["context"])
        self.assertEqual(self.agent.message_handler.users, [])
        decision_context = str(self.agent.decisions[0].get("context") or "")
        self.assertIn("[room context]", decision_context)
        self.assertIn("present:", decision_context)

    async def test_join_same_world_is_idempotent(self):
        inhabitant = WorldInhabitant(self.agent, member_id="agent1", display_name="一号")
        await inhabitant.join(world_url=self.url)
        try:
            await self._wait_present()
            async with WorldClient(self.url, member_id="alice") as alice:
                await alice.join()
                await alice.wait_for(lambda m: m.get("type") == "snapshot")
                await inhabitant.join(world_url=self.url)
                with self.assertRaises(TimeoutError):
                    await alice.wait_for(
                        lambda m: m.get("type") == "event"
                        and m.get("kind") in {"leave", "join"}
                        and m.get("actor_id") == "agent1",
                        timeout=0.6,
                    )
        finally:
            await inhabitant.leave()

    async def test_observes_own_join_and_leave(self):
        inhabitant = WorldInhabitant(self.agent, member_id="agent1", display_name="一号")
        await inhabitant.join(world_url=self.url)
        try:
            await self._wait_present()
            for _ in range(50):
                if self.agent.observed:
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(self.agent.observed[0]["context"], "一号 joined 大厅")
            self.assertEqual(self.agent.observed[0]["metadata"]["actor_id"], "agent1")
        finally:
            await inhabitant.leave()
        self.assertEqual(self.agent.observed[-1]["context"], "一号 left 大厅")
        self.assertEqual(self.agent.observed[-1]["metadata"]["actor_id"], "agent1")

    async def test_join_leave_observations_name_the_world(self):
        inhabitant = WorldInhabitant(self.agent, member_id="agent1", display_name="一号")
        inhabitant.world_id = "my-world"
        inhabitant.world_name = "my-world"
        await inhabitant._observe({"actor_id": "player2", "kind": "join", "seq": 1}, event_type="join")
        await inhabitant._observe({"actor_id": "human", "kind": "leave", "seq": 2}, event_type="leave")
        self.assertEqual(self.agent.observed[0]["context"], "player2 joined my-world")
        self.assertEqual(self.agent.observed[1]["context"], "human left my-world")
        self.assertEqual(self.agent.observed[0]["metadata"]["world_name"], "my-world")

    async def test_silence_stores_utterance_as_observation(self):
        self.agent.should_reply = False
        inhabitant = WorldInhabitant(self.agent, member_id="agent1", display_name="一号")
        await inhabitant.join(world_url=self.url)
        try:
            await self._wait_present()
            async with WorldClient(self.url, member_id="alice") as alice:
                await alice.join()
                await alice.wait_for(lambda m: m.get("type") == "snapshot")
                await alice.speak( "the coffee is hot")
                for _ in range(40):
                    if any("the coffee is hot" in str(item.get("context") or "") for item in self.agent.observed):
                        break
                    await asyncio.sleep(0.05)
        finally:
            await inhabitant.leave()
        self.assertEqual(self.agent.chats, [])
        self.assertEqual(self.agent.message_handler.users, [])
        heard = next(
            item for item in self.agent.observed if "the coffee is hot" in str(item.get("context") or "")
        )
        self.assertEqual(heard["context"], "the coffee is hot")
        self.assertEqual(heard["event_type"], "utterance")
        self.assertEqual(heard["channel"], "world")
        self.assertEqual(heard["room_name"], "大厅")
        self.assertEqual(heard["user_id"], "alice")
        self.assertEqual(heard["metadata"]["sender_name"], "alice")
        self.assertEqual(self.agent.decisions[0]["metadata"]["addressed_to_agent"], False)
        self.assertEqual(self.agent.decisions[0]["metadata"]["recently_spoke"], False)

    async def test_hears_file_and_passes_attachments_to_chat(self):
        inhabitant = WorldInhabitant(self.agent, member_id="agent1", display_name="一号")
        await inhabitant.join(world_url=self.url)
        try:
            await self._wait_present()
            async with WorldClient(self.url, member_id="alice") as alice:
                await alice.join()
                await alice.wait_for(lambda m: m.get("type") == "snapshot")
                await alice.speak(
                    "see this",
                    attachments=[{"name": "note.txt", "mime": "text/plain", "data": b"hello-file"}],
                )
                heard = await alice.wait_for(
                    lambda m: m.get("type") == "event"
                    and m.get("kind") == "utterance"
                    and m.get("actor_id") == "agent1",
                    timeout=8.0,
                )
                self.assertEqual(heard.get("text"), "I heard that")
        finally:
            await inhabitant.leave()
        chat = self.agent.chats[0]
        self.assertEqual(chat["user_message"], "see this [shared note.txt]")
        self.assertEqual(chat.get("inbox_kind"), InboxKind.PRESENCE_TURN)
        self.assertEqual(self.agent.message_handler.users, [])
        attachments = chat.get("attachments") or []
        self.assertEqual(len(attachments), 1)
        self.assertTrue(str(attachments[0].get("file_name") or "").startswith("note"))
        self.assertTrue(str(attachments[0].get("path") or "").startswith("assets/inbound/world/files/"))
        saved = self.workspace / attachments[0]["path"]
        self.assertEqual(saved.read_bytes(), b"hello-file")

    async def test_speaks_workspace_file_named_in_reply(self):
        (self.workspace / "hello.txt").write_text("hi from agent", encoding="utf-8")

        async def chat(user_message, user_id="", **kwargs):
            self.agent.chats.append({"user_message": user_message, "user_id": user_id, **kwargs})
            return "resent `hello.txt`"

        self.agent.chat = chat
        inhabitant = WorldInhabitant(self.agent, member_id="agent1", display_name="一号")
        await inhabitant.join(world_url=self.url)
        try:
            await self._wait_present()
            async with WorldClient(self.url, member_id="alice") as alice:
                await alice.join()
                await alice.wait_for(lambda m: m.get("type") == "snapshot")
                await alice.speak( "send the file")
                heard = await alice.wait_for(
                    lambda m: m.get("type") == "event"
                    and m.get("kind") == "utterance"
                    and m.get("actor_id") == "agent1",
                    timeout=8.0,
                )
        finally:
            await inhabitant.leave()
        atts = heard.get("attachments") or []
        self.assertEqual(len(atts), 1)
        self.assertEqual(atts[0]["name"], "hello.txt")
        self.assertEqual(base64.b64decode(atts[0]["data"]), b"hi from agent")

    async def test_silence_stores_file_attachment(self):
        self.agent.should_reply = False
        inhabitant = WorldInhabitant(self.agent, member_id="agent1", display_name="一号")
        await inhabitant.join(world_url=self.url)
        try:
            await self._wait_present()
            async with WorldClient(self.url, member_id="alice") as alice:
                await alice.join()
                await alice.wait_for(lambda m: m.get("type") == "snapshot")
                await alice.speak(
                    "",
                    attachments=[{"name": "photo.png", "mime": "image/png", "data": b"\x89PNG"}],
                )
                for _ in range(80):
                    if any("photo.png" in str(item.get("context") or "") for item in self.agent.observed):
                        break
                    await asyncio.sleep(0.05)
        finally:
            await inhabitant.leave()
        self.assertEqual(self.agent.chats, [])
        self.assertEqual(self.agent.message_handler.users, [])
        heard = next(
            item for item in self.agent.observed if "photo.png" in str(item.get("context") or "")
        )
        self.assertIn("shared photo.png", heard["context"])
        attachments = (heard.get("metadata") or {}).get("attachments") or []
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["kind"], "image")
        saved = self.workspace / attachments[0]["path"]
        self.assertEqual(saved.read_bytes(), b"\x89PNG")


class WorldAddressTests(unittest.TestCase):
    def setUp(self):
        self.inhabitant = WorldInhabitant(StubAgent(), member_id="aaac", display_name="Aaac")

    def test_listening_pause_shorter_when_named(self):
        inhabitant = WorldInhabitant(StubAgent(), member_id="agent2", display_name="二号")
        inhabitant._present = {"human": "Human", "agent2": "二号", "a": "A", "b": "B"}
        with patch("xagent.integrations.world.inhabitant.random.uniform", side_effect=lambda lo, hi: hi):
            named_hi = inhabitant._listening_pause_seconds({"text": "@agent2 hi"})
            ambient_hi = inhabitant._listening_pause_seconds({"text": "anyone here?"})
        self.assertLess(named_hi, ambient_hi)

    def test_only_mentions_and_at_names_count_as_self(self):
        self.assertTrue(self.inhabitant._addressed_to_self({"text": "@aaac are you there"}))
        self.assertTrue(self.inhabitant._addressed_to_self({"text": "hey @Aaac"}))
        self.assertTrue(self.inhabitant._addressed_to_self({"text": "hi", "mentions": ["aaac"]}))
        self.assertFalse(self.inhabitant._addressed_to_self({"text": "aaac are you there"}))
        self.assertFalse(self.inhabitant._addressed_to_self({"text": "hey Aaac"}))
        self.assertFalse(self.inhabitant._addressed_to_self({"text": "anyone here ?"}))
        self.assertFalse(self.inhabitant._addressed_to_self({"text": "hey"}))
        self.assertFalse(self.inhabitant._addressed_to_self({"text": "有人吗"}))
        self.assertFalse(self.inhabitant._addressed_to_self({"text": "the coffee is hot"}))
        self.assertFalse(self.inhabitant._addressed_to_self({"text": "maybe be shorter"}))

    def test_recently_spoke_is_read_from_the_log(self):
        self.inhabitant._recent = [
            {"kind": "utterance", "actor_id": "aaac", "text": "a long take", "seq": 1},
            {"kind": "utterance", "actor_id": "Jun", "text": "ok keep going", "seq": 2},
        ]
        self.assertTrue(self.inhabitant._recently_spoke({"text": "and another thing", "seq": 3}))
        other = WorldInhabitant(StubAgent(), member_id="player1", display_name="player1")
        other._recent = list(self.inhabitant._recent)
        self.assertFalse(other._recently_spoke({"text": "the coffee is hot", "seq": 3}))

    def test_speaker_label_is_one_name(self):
        self.inhabitant._names["testest"] = "Jun"
        self.inhabitant._names["player2"] = "Player2"
        self.inhabitant._names["aaac"] = "Aaac"
        self.assertEqual(self.inhabitant._speaker_label("testest"), "Jun")
        self.assertEqual(self.inhabitant._speaker_label("player1"), "player1")
        self.assertEqual(self.inhabitant._speaker_label("player2"), "Player2")
        self.inhabitant._present = {"player2": "Player2", "aaac": "Aaac"}
        self.assertEqual(self.inhabitant._present_labels(), ["Player2", "Aaac"])


def _world_task(*, content: str, world_url: str = "", user_id: str = "Jun"):
    target = {"world_url": world_url, "user_id": user_id}
    return SimpleNamespace(
        kind="task",
        delivery_channel="world",
        delivery={"channel": "world", "target": target, "user_id": user_id},
        target=target,
        delivery_user_id=user_id,
        task_type="message",
        content=content,
        task_id="task-1",
        name="task-1.json",
        run_at=datetime.now(),
    )


class WorldReminderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.hub = WorldHub.create(
            host="127.0.0.1",
            port=0,
            data_root=self._tmpdir.name,
        )
        self.hub.create_world(world_id="mind-venue", name="大厅")
        self.port = await self.hub.start()
        self.url = f"ws://127.0.0.1:{self.port}/ws/mind-venue"
        self.agent = StubAgent()

    async def asyncTearDown(self):
        await self.hub.stop()
        self._tmpdir.cleanup()

    async def _join(self) -> WorldInhabitant:
        inhabitant = WorldInhabitant(self.agent, member_id="agent1", display_name="一号")
        await inhabitant.join(world_url=self.url)
        world = self.hub.get("mind-venue")
        for _ in range(50):
            session = world._sessions.get("agent1") if world else None
            if session is not None and session.present:
                return inhabitant
            await asyncio.sleep(0.05)
        return inhabitant

    async def test_world_chat_exposes_delivery_context(self):
        captured = {}

        async def chat(user_message, user_id="", **kwargs):
            ctx = current_delivery_context()
            captured["channel"] = None if ctx is None else ctx.channel
            captured["world_id"] = None if ctx is None else ctx.target.get("world_id")
            captured["world_url"] = None if ctx is None else ctx.target.get("world_url")
            self.agent.chats.append({"user_message": user_message, "user_id": user_id, **kwargs})
            return "I heard that"

        self.agent.chat = chat
        inhabitant = await self._join()
        try:
            async with WorldClient(self.url, member_id="alice") as alice:
                await alice.join()
                await alice.wait_for(lambda m: m.get("type") == "snapshot")
                await alice.speak( "hello hall")
                await alice.wait_for(
                    lambda m: m.get("type") == "event"
                    and m.get("kind") == "utterance"
                    and m.get("actor_id") == "agent1",
                    timeout=8.0,
                )
        finally:
            await inhabitant.leave()
        self.assertEqual(captured.get("channel"), "world")
        self.assertEqual(captured.get("world_id"), "mind-venue")
        self.assertEqual(captured.get("world_url"), self.url)

    async def test_due_reminder_speaks_only_when_present(self):
        inhabitant = await self._join()
        try:
            self.assertTrue(
                inhabitant.can_handle_scheduled_task(
                    _world_task(content="Jun, 喝水", world_url=self.url)
                )
            )
            async with WorldClient(self.url, member_id="alice") as alice:
                await alice.join()
                await alice.wait_for(lambda m: m.get("type") == "snapshot")
                await inhabitant.dispatch_scheduled_task(
                    _world_task(content="Jun, 喝水", world_url=self.url)
                )
                heard = await alice.wait_for(
                    lambda m: m.get("type") == "event"
                    and m.get("kind") == "utterance"
                    and m.get("actor_id") == "agent1",
                    timeout=8.0,
                )
                self.assertEqual(heard.get("text"), "Jun, 喝水")
        finally:
            await inhabitant.leave()
        self.assertFalse(
            inhabitant.can_handle_scheduled_task(
                _world_task(content="Jun, 喝水", world_url=self.url)
            )
        )


class WorldReminderToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_reminder_from_world_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tool = create_schedule_task_tool(tasks_dir=tmpdir)
            inhabitant = WorldInhabitant(StubAgent(), member_id="player1")
            inhabitant.world_url = "ws://127.0.0.1:7182"
            inhabitant.world_id = "plaza"
            with scheduled_delivery_context(inhabitant.delivery_context(user_id="Jun")):
                created = await tool(
                    action="create",
                    task_type="message",
                    content="Jun, 喝水",
                    delay_seconds=60,
                )
            self.assertTrue(created["ok"])
            self.assertEqual(created["task"]["channel"], "world")
            self.assertEqual(created["task"]["target"]["world_id"], "plaza")
            self.assertNotIn("room_id", created["task"]["target"])
            self.assertEqual(created["task"]["user_id"], "Jun")


class WorldJoinRouteTests(unittest.TestCase):
    def test_join_and_status_and_leave(self):
        with tempfile.TemporaryDirectory() as raw:
            config_dir = Path(raw)
            (config_dir / "config.yaml").write_text("{}\n", encoding="utf-8")
            server = AgentHTTPServer(config_dir=str(config_dir), agent=StubAgent())
            with TestClient(server.app) as client:
                missing = client.post("/world/join", json={})
                self.assertEqual(missing.status_code, 422)
                joined = client.post(
                    "/world/join",
                    json={"world_url": "ws://127.0.0.1:9", "member_id": "agent1"},
                )
                self.assertEqual(joined.status_code, 200)
                body = joined.json()
                self.assertEqual(body["member_id"], "agent1")
                self.assertTrue(body["connected"])
                status = client.get("/world/status").json()
                self.assertEqual(status["world_url"], "ws://127.0.0.1:9")
                self.assertNotIn("room_id", status)
                left = client.post("/world/leave").json()
                self.assertFalse(left["connected"])
                presence = read_world_presence(config_dir)
                self.assertIsNotNone(presence)
                self.assertFalse(presence["want_present"])
                self.assertEqual(presence["world_url"], "ws://127.0.0.1:9")

    def test_api_autojoins_persisted_world_on_startup(self):
        with tempfile.TemporaryDirectory() as raw:
            config_dir = Path(raw)
            (config_dir / "config.yaml").write_text("{}\n", encoding="utf-8")
            mark_world_presence(
                config_dir,
                world_url="ws://127.0.0.1:9/ws/plaza",
                member_id="agent1",
                display_name="Agent One",
                world_id="plaza",
                want_present=True,
            )
            server = AgentHTTPServer(config_dir=str(config_dir), agent=StubAgent())
            with TestClient(server.app) as client:
                status = client.get("/world/status").json()
            self.assertEqual(status["member_id"], "agent1")
            self.assertEqual(status["world_url"], "ws://127.0.0.1:9/ws/plaza")
            self.assertIsNotNone(server.world_inhabitant)

    def test_api_skips_autojoin_when_disabled(self):
        with tempfile.TemporaryDirectory() as raw:
            config_dir = Path(raw)
            (config_dir / "config.yaml").write_text(
                "provider:\n  name: openai\n  api_key: test-key\n  model: gpt-5.4-mini\n"
                "world:\n  autojoin: false\n",
                encoding="utf-8",
            )
            mark_world_presence(
                config_dir,
                world_url="ws://127.0.0.1:9/ws/plaza",
                member_id="agent1",
                want_present=True,
            )
            server = AgentHTTPServer(config_dir=str(config_dir), agent=StubAgent())
            with TestClient(server.app) as client:
                status = client.get("/world/status").json()
            self.assertFalse(status["connected"])
            self.assertIsNone(server.world_inhabitant)


if __name__ == "__main__":
    unittest.main()
