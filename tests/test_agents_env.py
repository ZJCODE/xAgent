"""Tests for the independent agents_env world (no xagent imports)."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from agents_env.models import EventKind
from agents_env.scene import SceneConfig, parse_relative_delay, parse_scene_config
from agents_env.store import open_store_for_scene
from agents_env.world import World


def _plaza(world_id: str = "test-plaza") -> SceneConfig:
    return parse_scene_config(
        {
            "world": world_id,
            "rooms": [
                {
                    "id": "hall",
                    "name": "大厅",
                    "setting": "开放大厅",
                }
            ],
            "scenes": [{"at": "+1s", "room": "hall", "text": "天色暗下来了"}],
        }
    )


class Capture:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, raw: str) -> None:
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


class WorldPhysicsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.scene = _plaza(world_id="physics")
        self.store = open_store_for_scene(self.scene, root=self.root)
        self.world = World(self.store, self.scene)
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
        return cap, session

    async def test_two_members_hear_each_other(self):
        alice_cap, alice = await self._attach("alice", "Alice")
        bob_cap, bob = await self._attach("bob", "Bob")

        await self.world.handle(alice, "join", {"room_id": "hall"})
        await self.world.handle(bob, "join", {"room_id": "hall"})
        await self.world.handle(alice, "speak", {"room_id": "hall", "text": "hi bob"})

        alice_texts = [e["text"] for e in alice_cap.events() if e["kind"] == "utterance"]
        bob_texts = [e["text"] for e in bob_cap.events() if e["kind"] == "utterance"]
        self.assertIn("hi bob", alice_texts)  # self-echo
        self.assertIn("hi bob", bob_texts)

    async def test_leave_stops_live_events(self):
        alice_cap, alice = await self._attach("alice")
        bob_cap, bob = await self._attach("bob")
        await self.world.handle(alice, "join", {"room_id": "hall"})
        await self.world.handle(bob, "join", {"room_id": "hall"})

        await self.world.handle(bob, "leave", {"room_id": "hall"})
        before = len([e for e in bob_cap.events() if e["kind"] == "utterance"])
        await self.world.handle(alice, "speak", {"room_id": "hall", "text": "still here?"})
        after = [e for e in bob_cap.events() if e["kind"] == "utterance"]
        self.assertEqual(len(after), before)
        alice_utt = [e for e in alice_cap.events() if e.get("text") == "still here?"]
        self.assertEqual(len(alice_utt), 1)

    async def test_scene_event_fires(self):
        alice_cap, alice = await self._attach("alice")
        await self.world.handle(alice, "join", {"room_id": "hall"})
        await asyncio.sleep(1.2)
        scenes = [e for e in alice_cap.events() if e["kind"] == "scene"]
        self.assertTrue(any(e["text"] == "天色暗下来了" for e in scenes))
        self.assertTrue(all(e["actor_id"] == "world" for e in scenes))

    async def test_log_survives_reopen(self):
        alice_cap, alice = await self._attach("alice")
        await self.world.handle(alice, "join", {"room_id": "hall"})
        await self.world.handle(alice, "speak", {"room_id": "hall", "text": "remember me"})
        await self.world.close()

        store2 = open_store_for_scene(self.scene, root=self.root)
        events = store2.recent_events("hall", limit=50)
        store2.close()
        self.assertTrue(any(e.kind == EventKind.UTTERANCE and e.text == "remember me" for e in events))

    async def test_no_xagent_import(self):
        import agents_env
        import agents_env.world as world_mod
        import agents_env.store as store_mod
        import agents_env.server as server_mod

        for mod in (agents_env, world_mod, store_mod, server_mod):
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
        scene = _plaza(world_id="ws-venue")
        # Faster scene for WS test via immediate emit; use no scheduled wait.
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
        await self.server.world.start()
        from websockets.asyncio.server import serve

        self._ws_server = await serve(self.server._handler, "127.0.0.1", 0)
        socks = self._ws_server.sockets
        assert socks
        self.port = socks[0].getsockname()[1]

    async def asyncTearDown(self):
        self._ws_server.close()
        await self._ws_server.wait_closed()
        await self.server.world.close()
        self._tmpdir.cleanup()

    async def test_dummy_style_roundtrip(self):
        from websockets.asyncio.client import connect

        url = f"ws://127.0.0.1:{self.port}"

        async def connect_and_join(member_id: str):
            ws = await connect(url)
            await ws.send(
                json.dumps(
                    {
                        "type": "hello",
                        "member_id": member_id,
                        "display_name": member_id,
                    }
                )
            )
            welcome = json.loads(await ws.recv())
            self.assertEqual(welcome.get("type"), "welcome")
            await ws.send(json.dumps({"type": "join", "room_id": "hall"}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("type") == "snapshot":
                    break
            return ws

        alice = await connect_and_join("alice")
        bob = await connect_and_join("bob")

        await alice.send(
            json.dumps({"type": "speak", "room_id": "hall", "text": "hello from alice"})
        )

        heard = None
        for _ in range(20):
            msg = json.loads(await asyncio.wait_for(bob.recv(), timeout=2.0))
            if msg.get("type") == "event" and msg.get("kind") == "utterance":
                heard = msg.get("text")
                if heard == "hello from alice":
                    break

        await alice.close()
        await bob.close()
        self.assertEqual(heard, "hello from alice")


if __name__ == "__main__":
    unittest.main()
