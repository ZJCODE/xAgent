"""Tests for the independent agents_env world (no xagent imports)."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from agents_env import PROTOCOL_VERSION
from agents_env.clock import VirtualClock
from agents_env.models import EventKind
from agents_env.paths import world_data_dir
from agents_env.scene import SceneConfig, parse_relative_delay, parse_scene_config
from agents_env.store import open_store_for_scene
from agents_env.world import World


def _plaza(world_id: str = "test-plaza", *, scenes: list | None = None) -> SceneConfig:
    body = {
        "world": world_id,
        "rooms": [
            {
                "id": "hall",
                "name": "大厅",
                "setting": "开放大厅",
            }
        ],
        "scenes": scenes if scenes is not None else [{"at": "+1s", "room": "hall", "text": "天色暗下来了"}],
    }
    return parse_scene_config(body)


def _two_rooms(world_id: str = "two-rooms") -> SceneConfig:
    return parse_scene_config(
        {
            "world": world_id,
            "rooms": [
                {"id": "hall", "name": "大厅", "setting": "开放大厅"},
                {"id": "quiet", "name": "侧厅", "setting": "安静"},
            ],
            "scenes": [],
        }
    )


class Capture:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.delay = 0.0
        self.fail_after = 0
        self._sends = 0

    async def send(self, raw: str) -> None:
        self._sends += 1
        if self.fail_after and self._sends > self.fail_after:
            raise ConnectionResetError("socket gone")
        if self.delay:
            await asyncio.sleep(self.delay)
        self.messages.append(json.loads(raw))

    def events(self) -> list[dict]:
        return [m for m in self.messages if m.get("type") == "event"]

    def of_type(self, msg_type: str) -> list[dict]:
        return [m for m in self.messages if m.get("type") == msg_type]


class SceneParseTests(unittest.TestCase):
    def test_relative_delay(self):
        self.assertEqual(parse_relative_delay("+5m"), 300.0)
        self.assertEqual(parse_relative_delay("+1h30m"), 5400.0)
        self.assertEqual(parse_relative_delay("+10s"), 10.0)

    def test_rejects_persona_free_but_requires_rooms(self):
        with self.assertRaises(ValueError):
            parse_scene_config({"world": "x", "rooms": []})

    def test_rejects_path_like_world_id(self):
        with self.assertRaises(ValueError):
            parse_scene_config({"world": "..", "rooms": [{"id": "hall", "name": "h"}]})
        with self.assertRaises(ValueError):
            parse_scene_config(
                {"world": "../../tmp/escaped", "rooms": [{"id": "hall", "name": "h"}]}
            )


class PathSafetyTests(unittest.TestCase):
    def test_world_data_dir_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                world_data_dir("..", root=root)
            with self.assertRaises(ValueError):
                world_data_dir("../../tmp/escaped", root=root)
            path = world_data_dir("plaza", root=root)
            self.assertTrue(str(path.resolve()).startswith(str(root.resolve())))


class WorldPhysicsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.clock = VirtualClock(start=1_000.0)
        self.scene = _plaza(world_id="physics")
        self.store = open_store_for_scene(self.scene, root=self.root, clock=self.clock)
        self.world = World(self.store, self.scene, clock=self.clock)
        await self.world.start()

    async def asyncTearDown(self):
        await self.world.close()
        self._tmpdir.cleanup()

    async def _attach(self, member_id: str, name: str | None = None) -> tuple[Capture, object]:
        cap = Capture()
        session = await self.world.attach(
            member_id=member_id,
            display_name=name or member_id,
            send=cap.send,
        )
        await self.world.welcome(session)
        await self.world.wait_idle(session)
        return cap, session

    async def _act(self, session, msg_type: str, payload: dict, *wait_sessions) -> None:
        await self.world.handle(session, msg_type, payload)
        targets = wait_sessions or (session,)
        await self.world.wait_idle(*targets)

    async def _tick(self, seconds: float = 0.0) -> None:
        if seconds:
            self.clock.advance(seconds)
        for _ in range(8):
            await asyncio.sleep(0)
        await self.world.wait_idle()

    async def test_two_members_hear_each_other(self):
        alice_cap, alice = await self._attach("alice", "Alice")
        bob_cap, bob = await self._attach("bob", "Bob")

        await self._act(alice, "join", {"room_id": "hall"}, alice, bob)
        await self._act(bob, "join", {"room_id": "hall"}, alice, bob)
        await self._act(alice, "speak", {"room_id": "hall", "text": "hi bob"}, alice, bob)

        alice_texts = [e["text"] for e in alice_cap.events() if e["kind"] == "utterance"]
        bob_texts = [e["text"] for e in bob_cap.events() if e["kind"] == "utterance"]
        self.assertIn("hi bob", alice_texts)  # self-echo
        self.assertIn("hi bob", bob_texts)

    async def test_leave_stops_live_events(self):
        alice_cap, alice = await self._attach("alice")
        bob_cap, bob = await self._attach("bob")
        await self._act(alice, "join", {"room_id": "hall"}, alice, bob)
        await self._act(bob, "join", {"room_id": "hall"}, alice, bob)

        await self._act(bob, "leave", {"room_id": "hall"}, alice, bob)
        before = len([e for e in bob_cap.events() if e["kind"] == "utterance"])
        await self._act(alice, "speak", {"room_id": "hall", "text": "still here?"}, alice)
        after = [e for e in bob_cap.events() if e["kind"] == "utterance"]
        self.assertEqual(len(after), before)
        alice_utt = [e for e in alice_cap.events() if e.get("text") == "still here?"]
        self.assertEqual(len(alice_utt), 1)

    async def test_scene_event_fires_on_virtual_clock(self):
        alice_cap, alice = await self._attach("alice")
        await self._act(alice, "join", {"room_id": "hall"})
        await self._tick(1.0)
        scenes = [e for e in alice_cap.events() if e["kind"] == "scene"]
        self.assertTrue(any(e["text"] == "天色暗下来了" for e in scenes))
        self.assertTrue(all(e["actor_id"] == "world" for e in scenes))

    async def test_scene_does_not_replay_on_restart(self):
        alice_cap, alice = await self._attach("alice")
        await self._act(alice, "join", {"room_id": "hall"})
        await self._tick(1.0)
        first = [e for e in self.store.recent_events("hall", limit=50) if e.kind == EventKind.SCENE]
        self.assertEqual(len(first), 1)
        await self.world.close()

        clock2 = VirtualClock(start=self.clock.now())
        store2 = open_store_for_scene(self.scene, root=self.root, clock=clock2)
        world2 = World(store2, self.scene, clock=clock2)
        await world2.start()
        clock2.advance(5.0)
        for _ in range(8):
            await asyncio.sleep(0)
        second = [e for e in store2.recent_events("hall", limit=50) if e.kind == EventKind.SCENE]
        await world2.close()
        self.assertEqual(len(second), 1)

    async def test_log_survives_reopen(self):
        alice_cap, alice = await self._attach("alice")
        await self._act(alice, "join", {"room_id": "hall"})
        await self._act(alice, "speak", {"room_id": "hall", "text": "remember me"})
        await self.world.close()

        store2 = open_store_for_scene(self.scene, root=self.root, clock=self.clock)
        events = store2.recent_events("hall", limit=50)
        store2.close()
        self.assertTrue(any(e.kind == EventKind.UTTERANCE and e.text == "remember me" for e in events))

    async def test_welcome_advertises_protocol_and_heads(self):
        cap, session = await self._attach("alice")
        welcome = cap.of_type("welcome")[0]
        self.assertEqual(welcome.get("protocol_version"), PROTOCOL_VERSION)
        self.assertIn("present_rooms", welcome)
        self.assertIn("latest_seq", welcome["rooms"][0])
        self.assertIn("latest_room_seq", welcome["rooms"][0])

    async def test_join_is_not_duplicated_for_joiner(self):
        cap, session = await self._attach("alice")
        await self._act(session, "join", {"room_id": "hall"})
        live = [m for m in cap.events() if m.get("kind") == "join"]
        snap = cap.of_type("snapshot")[-1]
        snap_joins = [e for e in snap["events"] if e.get("kind") == "join"]
        self.assertEqual(live, [])
        self.assertEqual(len(snap_joins), 1)
        self.assertEqual(snap_joins[0].get("text"), "")

    async def test_room_seq_is_contiguous_per_room(self):
        scene = _two_rooms("seq-world")
        await self.world.close()
        self.store = open_store_for_scene(scene, root=self.root, clock=self.clock)
        self.world = World(self.store, scene, clock=self.clock)
        await self.world.start()
        alice_cap, alice = await self._attach("alice")
        await self._act(alice, "join", {"room_id": "hall"})
        await self._act(alice, "join", {"room_id": "quiet"})
        await self._act(alice, "speak", {"room_id": "hall", "text": "a"})
        await self._act(alice, "speak", {"room_id": "quiet", "text": "b"})
        await self._act(alice, "speak", {"room_id": "hall", "text": "c"})
        hall = self.store.recent_events("hall", limit=50)
        hall_room_seq = [e.room_seq for e in hall]
        self.assertEqual(hall_room_seq, list(range(1, len(hall_room_seq) + 1)))
        self.assertTrue(any(e.seq != e.room_seq for e in hall))

    async def test_sync_reports_has_more(self):
        _, alice = await self._attach("alice")
        await self._act(alice, "join", {"room_id": "hall"})
        for i in range(210):
            self.store.append_event(
                room_id="hall",
                kind=EventKind.UTTERANCE,
                actor_id="alice",
                text=f"flood {i}",
            )
        cap = Capture()
        syncer = await self.world.attach(member_id="syncer", display_name="syncer", send=cap.send)
        await self.world.welcome(syncer)
        await self._act(syncer, "sync", {"room_id": "hall", "after_seq": 0})
        snap = [m for m in cap.messages if m.get("type") == "snapshot"][-1]
        self.assertTrue(snap.get("sync"))
        self.assertEqual(len(snap["events"]), 200)
        self.assertTrue(snap.get("has_more"))
        self.assertEqual(snap.get("next_after_seq"), snap["events"][-1]["seq"])

    async def test_malformed_after_seq_does_not_drop_session(self):
        cap, alice = await self._attach("alice")
        await self._act(alice, "join", {"room_id": "hall"})
        await self._act(alice, "sync", {"room_id": "hall", "after_seq": "abc"})
        err = cap.of_type("error")[-1]
        self.assertEqual(err.get("code"), "bad_payload")
        await self._act(alice, "speak", {"room_id": "hall", "text": "still here"})
        texts = [e["text"] for e in cap.events() if e.get("kind") == "utterance"]
        self.assertIn("still here", texts)

    async def test_dead_socket_emits_leave(self):
        alice_cap, alice = await self._attach("alice")
        broken_cap, broken = await self._attach("broken")
        await self._act(alice, "join", {"room_id": "hall"}, alice, broken)
        await self._act(broken, "join", {"room_id": "hall"}, alice, broken)
        broken_cap.fail_after = broken_cap._sends
        await self.world.handle(alice, "speak", {"room_id": "hall", "text": "trigger"})
        for _ in range(40):
            leaves = [e for e in alice_cap.events() if e.get("kind") == "leave"]
            if leaves:
                break
            await asyncio.sleep(0.01)
        await self.world.wait_idle(alice)
        self.assertFalse(self.store.is_present("broken", "hall"))
        self.assertTrue(any(e.get("kind") == "leave" and e.get("actor_id") == "broken" for e in alice_cap.events()))

    async def test_slow_consumer_does_not_stall_other_rooms(self):
        await self.world.close()
        scene = _two_rooms("stall-world")
        self.store = open_store_for_scene(scene, root=self.root, clock=self.clock)
        self.world = World(self.store, scene, clock=self.clock)
        await self.world.start()

        slow_cap, slow = await self._attach("slow")
        fast_cap, fast = await self._attach("fast")
        other_cap, other = await self._attach("other")
        await self._act(slow, "join", {"room_id": "hall"}, slow, fast)
        await self._act(fast, "join", {"room_id": "hall"}, slow, fast)
        await self._act(other, "join", {"room_id": "quiet"}, other)

        slow_cap.delay = 2.0
        t0 = time.perf_counter()
        await self.world.handle(fast, "speak", {"room_id": "hall", "text": "hi hall"})
        await self.world.handle(other, "speak", {"room_id": "quiet", "text": "unrelated room"})
        await other.outbound.join()
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 0.5)
        other_texts = [e["text"] for e in other_cap.events() if e.get("kind") == "utterance"]
        self.assertIn("unrelated room", other_texts)
        slow_cap.delay = 0.0

    async def test_rate_limit_uses_clock(self):
        cap, alice = await self._attach("alice")
        await self._act(alice, "join", {"room_id": "hall"})
        for i in range(10):
            await self._act(alice, "speak", {"room_id": "hall", "text": f"n{i}"})
        await self._act(alice, "speak", {"room_id": "hall", "text": "too many"})
        err = cap.of_type("error")[-1]
        self.assertEqual(err.get("code"), "rate_limited")
        self.clock.advance(1.1)
        await self._act(alice, "speak", {"room_id": "hall", "text": "after window"})
        texts = [e["text"] for e in cap.events() if e.get("kind") == "utterance"]
        self.assertIn("after window", texts)

    async def test_events_index_exists(self):
        names = {
            row["name"]
            for row in self.store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        self.assertIn("idx_events_room_seq", names)
        mode = self.store._conn.execute("PRAGMA synchronous").fetchone()[0]
        self.assertEqual(int(mode), 1)  # NORMAL

    async def test_no_xagent_import(self):
        import agents_env
        import agents_env.client as client_mod
        import agents_env.clock as clock_mod
        import agents_env.server as server_mod
        import agents_env.store as store_mod
        import agents_env.world as world_mod

        for mod in (agents_env, world_mod, store_mod, server_mod, client_mod, clock_mod):
            for name in dir(mod):
                obj = getattr(mod, name)
                module_name = getattr(obj, "__module__", "") or ""
                self.assertFalse(
                    module_name.startswith("xagent."),
                    f"{mod.__name__}.{name} leaked xagent module {module_name}",
                )


class WebSocketVenueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        scene = parse_scene_config(
            {
                "world": "ws-venue",
                "rooms": [{"id": "hall", "name": "大厅", "setting": "ws"}],
                "scenes": [],
            }
        )
        from agents_env.server import WorldServer

        self.server = WorldServer.from_scene(
            scene,
            host="127.0.0.1",
            port=0,
            data_root=str(self.root),
        )
        self.port = await self.server.start()

    async def asyncTearDown(self):
        await self.server.stop()
        self._tmpdir.cleanup()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    async def test_dummy_style_roundtrip(self):
        from agents_env.client import WorldClient

        async with WorldClient(self.url, member_id="alice") as alice:
            async with WorldClient(self.url, member_id="bob") as bob:
                self.assertEqual(alice.welcome.get("protocol_version"), PROTOCOL_VERSION)
                await alice.join("hall")
                await bob.join("hall")
                await alice.wait_for(lambda m: m.get("type") == "snapshot")
                await bob.wait_for(lambda m: m.get("type") == "snapshot")
                await alice.speak("hall", "hello from alice")
                heard = await bob.wait_for(
                    lambda m: m.get("type") == "event" and m.get("kind") == "utterance"
                )
                self.assertEqual(heard.get("text"), "hello from alice")
                self.assertIn("room_seq", heard)

    async def test_malformed_sync_keeps_connection(self):
        from agents_env.client import WorldClient

        async with WorldClient(self.url, member_id="alice") as alice:
            await alice.join("hall")
            await alice.wait_for(lambda m: m.get("type") == "snapshot")
            await alice._send({"type": "sync", "room_id": "hall", "after_seq": "abc"})
            err = await alice.wait_for(lambda m: m.get("type") == "error")
            self.assertEqual(err.get("code"), "bad_payload")
            await alice.speak("hall", "still connected")
            echo = await alice.wait_for(
                lambda m: m.get("type") == "event" and m.get("text") == "still connected"
            )
            self.assertEqual(echo.get("kind"), "utterance")

    async def test_replaced_connection_is_closed(self):
        from agents_env.client import WorldClient

        first = WorldClient(self.url, member_id="bob")
        await first.connect()
        await first.join("hall")
        await first.wait_for(lambda m: m.get("type") == "snapshot")
        second = WorldClient(self.url, member_id="bob")
        await second.connect()
        msg = await first.wait_for(
            lambda m: m.get("type") in {"error", "_closed"}, timeout=3.0
        )
        if msg.get("type") == "error":
            self.assertEqual(msg.get("code"), "replaced")
        await asyncio.sleep(0.2)
        self.assertTrue(first._ws is None or first._ws.close_code is not None or first._ws.state.name != "OPEN")
        await second.close()
        await first.close()


if __name__ == "__main__":
    unittest.main()
