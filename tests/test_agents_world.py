"""Tests for the independent agents_world hub (no xagent imports)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from agents_world import PROTOCOL_VERSION
from agents_world.clock import VirtualClock
from agents_world.config import WorldConfig
from agents_world.models import EventKind
from agents_world.paths import world_data_dir
from agents_world.server import WorldHub
from agents_world.store import open_store_for_world
from agents_world.world import World


async def _http(port: int, method: str, path: str) -> tuple[int, dict[str, str], bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode()
    )
    await writer.drain()
    raw = await reader.read()
    writer.close()
    await writer.wait_closed()
    header_blob, _, body = raw.partition(b"\r\n\r\n")
    lines = header_blob.decode("iso-8859-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    return status, headers, body


async def _http_get(port: int, path: str) -> tuple[int, dict[str, str], bytes]:
    return await _http(port, "GET", path)


_ASSET_RE = re.compile(r"""(?:src|href)=["'](/assets/[^"']+)["']""")


def _asset_paths(html: str) -> list[str]:
    return _ASSET_RE.findall(html)


def _plaza(world_id: str = "test-plaza") -> WorldConfig:
    return WorldConfig.create(world_id=world_id, name=world_id)


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


class WorldConfigTests(unittest.TestCase):
    def test_create_defaults_name(self):
        cfg = WorldConfig.create(world_id="plaza")
        self.assertEqual(cfg.name, "plaza")

    def test_rejects_path_like_world_id(self):
        with self.assertRaises(ValueError):
            WorldConfig.create(world_id="..", name="h")
        with self.assertRaises(ValueError):
            WorldConfig.create(world_id="../../tmp/escaped", name="h")


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
            self.assertEqual(path.name, "plaza")
            self.assertEqual(path.parent.name, "worlds")

    def test_allocate_world_id_from_name(self):
        from agents_world.paths import WORLD_NAME_RULE, allocate_world_id, validate_world_id

        self.assertEqual(allocate_world_id("cafe", taken=()), "cafe")
        self.assertEqual(allocate_world_id("cafe", taken={"cafe"}), "cafe-2")
        self.assertEqual(allocate_world_id("plaza", taken={"plaza"}), "plaza-2")
        with self.assertRaises(ValueError) as ctx:
            validate_world_id("Cafe")
        self.assertEqual(str(ctx.exception), WORLD_NAME_RULE)
        with self.assertRaises(ValueError):
            validate_world_id("大厅")

    def test_world_data_dir_accepts_valid_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = world_data_dir("plaza", root=root)
            self.assertEqual(path.name, "plaza")
            self.assertEqual(path.parent.name, "worlds")


class WorldPhysicsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.clock = VirtualClock(start=1_000.0)
        self.config = _plaza(world_id="physics")
        self.store = open_store_for_world(self.config, root=self.root, clock=self.clock)
        self.world = World(self.store, self.config, clock=self.clock)
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

    async def test_two_members_hear_each_other(self):
        alice_cap, alice = await self._attach("alice", "Alice")
        bob_cap, bob = await self._attach("bob", "Bob")
        await self._act(alice, "join", {}, alice, bob)
        await self._act(bob, "join", {}, alice, bob)
        await self._act(alice, "speak", {"text": "hi bob"}, alice, bob)
        alice_texts = [e["text"] for e in alice_cap.events() if e["kind"] == "utterance"]
        bob_texts = [e["text"] for e in bob_cap.events() if e["kind"] == "utterance"]
        self.assertIn("hi bob", alice_texts)
        self.assertIn("hi bob", bob_texts)

    async def test_leave_stops_live_events(self):
        alice_cap, alice = await self._attach("alice")
        bob_cap, bob = await self._attach("bob")
        await self._act(alice, "join", {}, alice, bob)
        await self._act(bob, "join", {}, alice, bob)
        await self._act(bob, "leave", {}, alice, bob)
        before = len([e for e in bob_cap.events() if e["kind"] == "utterance"])
        await self._act(alice, "speak", {"text": "still here?"}, alice)
        after = [e for e in bob_cap.events() if e["kind"] == "utterance"]
        self.assertEqual(len(after), before)

    async def test_log_survives_reopen(self):
        _, alice = await self._attach("alice")
        await self._act(alice, "join", {})
        await self._act(alice, "speak", {"text": "remember me"})
        await self.world.close()
        store2 = open_store_for_world(self.config, root=self.root, clock=self.clock)
        events = store2.recent_events(limit=50)
        store2.close()
        self.assertTrue(any(e.kind == EventKind.UTTERANCE and e.text == "remember me" for e in events))

    async def test_welcome_advertises_protocol_and_heads(self):
        cap, _ = await self._attach("alice")
        welcome = cap.of_type("welcome")[0]
        self.assertEqual(welcome.get("protocol_version"), PROTOCOL_VERSION)
        self.assertEqual(welcome.get("name"), "physics")
        self.assertIn("latest_seq", welcome)
        self.assertNotIn("rooms", welcome)

    async def test_join_is_not_duplicated_for_joiner(self):
        cap, session = await self._attach("alice")
        await self._act(session, "join", {})
        live = [m for m in cap.events() if m.get("kind") == "join"]
        snap = cap.of_type("snapshot")[-1]
        snap_joins = [e for e in snap["events"] if e.get("kind") == "join"]
        self.assertEqual(live, [])
        self.assertEqual(len(snap_joins), 1)

    async def test_seq_is_contiguous(self):
        _, alice = await self._attach("alice")
        await self._act(alice, "join", {})
        await self._act(alice, "speak", {"text": "a"})
        await self._act(alice, "speak", {"text": "b"})
        await self._act(alice, "speak", {"text": "c"})
        events = self.store.recent_events(limit=50)
        seqs = [e.seq for e in events]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))

    async def test_sync_reports_has_more(self):
        _, alice = await self._attach("alice")
        await self._act(alice, "join", {})
        for i in range(210):
            self.store.append_event(
                kind=EventKind.UTTERANCE,
                actor_id="alice",
                text=f"flood {i}",
            )
        cap = Capture()
        syncer = await self.world.attach(member_id="syncer", display_name="syncer", send=cap.send)
        await self.world.welcome(syncer)
        await self._act(syncer, "sync", {"after_seq": 0})
        snap = [m for m in cap.messages if m.get("type") == "snapshot"][-1]
        self.assertTrue(snap.get("sync"))
        self.assertEqual(len(snap["events"]), 200)
        self.assertTrue(snap.get("has_more"))

    async def test_malformed_after_seq_does_not_drop_session(self):
        cap, alice = await self._attach("alice")
        await self._act(alice, "join", {})
        await self._act(alice, "sync", {"after_seq": "abc"})
        err = cap.of_type("error")[-1]
        self.assertEqual(err.get("code"), "bad_payload")
        await self._act(alice, "speak", {"text": "still here"})
        texts = [e["text"] for e in cap.events() if e.get("kind") == "utterance"]
        self.assertIn("still here", texts)

    async def test_dead_socket_emits_leave(self):
        alice_cap, alice = await self._attach("alice")
        broken_cap, broken = await self._attach("broken")
        await self._act(alice, "join", {}, alice, broken)
        await self._act(broken, "join", {}, alice, broken)
        broken_cap.fail_after = broken_cap._sends
        await self.world.handle(alice, "speak", {"text": "trigger"})
        for _ in range(40):
            leaves = [e for e in alice_cap.events() if e.get("kind") == "leave"]
            if leaves:
                break
            await asyncio.sleep(0.01)
        await self.world.wait_idle(alice)
        self.assertFalse(self.store.is_present("broken"))

    async def test_slow_consumer_does_not_stall_other_members(self):
        slow_cap, slow = await self._attach("slow")
        _, fast = await self._attach("fast")
        other_cap, other = await self._attach("other")
        await self._act(slow, "join", {}, slow, fast, other)
        await self._act(fast, "join", {}, slow, fast, other)
        await self._act(other, "join", {}, slow, fast, other)
        slow_cap.delay = 2.0
        t0 = time.perf_counter()
        await self.world.handle(fast, "speak", {"text": "hi everyone"})
        await self.world.handle(other, "speak", {"text": "unrelated line"})
        await other.outbound.join()
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 0.5)
        other_texts = [e["text"] for e in other_cap.events() if e.get("kind") == "utterance"]
        self.assertIn("unrelated line", other_texts)
        slow_cap.delay = 0.0

    async def test_speak_file_is_a_medium_event(self):
        alice_cap, alice = await self._attach("alice")
        bob_cap, bob = await self._attach("bob")
        await self._act(alice, "join", {}, alice, bob)
        await self._act(bob, "join", {}, alice, bob)
        payload = base64.b64encode(b"hello-file").decode("ascii")
        await self._act(
            alice,
            "speak",
            {
                "text": "see this",
                "attachments": [{"name": "note.txt", "mime": "text/plain", "data": payload}],
            },
            alice,
            bob,
        )
        heard = [e for e in bob_cap.events() if e.get("kind") == "utterance"][-1]
        atts = heard.get("attachments") or []
        self.assertEqual(atts[0]["url"], f"/worlds/physics/files/{atts[0]['id']}")
        self.assertEqual(base64.b64decode(atts[0]["data"]), b"hello-file")
        stored = [item.to_dict(world_id="physics") for item in self.store.recent_events(limit=5) if item.attachments]
        self.assertNotIn("data", stored[0]["attachments"][0])

    async def test_speak_attachments_without_text(self):
        cap, alice = await self._attach("alice")
        await self._act(alice, "join", {})
        payload = base64.b64encode(b"only-file").decode("ascii")
        await self._act(
            alice,
            "speak",
            {
                "text": "",
                "attachments": [{"name": "solo.bin", "mime": "application/octet-stream", "data": payload}],
            },
        )
        heard = [e for e in cap.events() if e.get("kind") == "utterance"][-1]
        self.assertEqual(heard["attachments"][0]["name"], "solo.bin")

    async def test_speak_rejects_oversize_file(self):
        cap, alice = await self._attach("alice")
        await self._act(alice, "join", {})
        payload = base64.b64encode(b"too-big").decode("ascii")
        with patch("agents_world.world.MAX_FILE_BYTES", 3):
            await self._act(
                alice,
                "speak",
                {"text": "x", "attachments": [{"name": "big.bin", "data": payload}]},
            )
        err = cap.of_type("error")[-1]
        self.assertEqual(err.get("code"), "file_too_large")

    async def test_rate_limit_uses_clock(self):
        cap, alice = await self._attach("alice")
        await self._act(alice, "join", {})
        for i in range(10):
            await self._act(alice, "speak", {"text": f"n{i}"})
        await self._act(alice, "speak", {"text": "too many"})
        err = cap.of_type("error")[-1]
        self.assertEqual(err.get("code"), "rate_limited")
        self.clock.advance(1.1)
        await self._act(alice, "speak", {"text": "after window"})
        texts = [e["text"] for e in cap.events() if e.get("kind") == "utterance"]
        self.assertIn("after window", texts)

    async def test_wal_and_synchronous(self):
        mode = self.store._conn.execute("PRAGMA synchronous").fetchone()[0]
        self.assertEqual(int(mode), 1)
        journal = self.store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(str(journal).lower(), "wal")

    async def test_no_xagent_import(self):
        import agents_world
        import agents_world.client as client_mod
        import agents_world.clock as clock_mod
        import agents_world.files as files_mod
        import agents_world.http as http_mod
        import agents_world.neighbors as neighbors_mod
        import agents_world.server as server_mod
        import agents_world.store as store_mod
        import agents_world.world as world_mod

        for mod in (agents_world, world_mod, store_mod, server_mod, client_mod, clock_mod, http_mod, neighbors_mod, files_mod):
            for name in dir(mod):
                obj = getattr(mod, name)
                module_name = getattr(obj, "__module__", "") or ""
                self.assertFalse(
                    module_name.startswith("xagent."),
                    f"{mod.__name__}.{name} leaked xagent module {module_name}",
                )


class WorldHubTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.hub = WorldHub.create(
            host="127.0.0.1",
            port=0,
            data_root=str(self.root),
        )
        os.environ["AGENTS_WORLD_NEIGHBORS_ROOT"] = str(self.root)
        self.port = await self.hub.start()

    async def asyncTearDown(self):
        await self.hub.stop()
        os.environ.pop("AGENTS_WORLD_NEIGHBORS_ROOT", None)
        self._tmpdir.cleanup()

    def _ws(self, world_id: str) -> str:
        return f"ws://127.0.0.1:{self.port}/ws/{quote(world_id, safe='')}"

    async def test_create_list_and_isolate_worlds(self):
        status, _, body = await _http_get(self.port, "/worlds/create?name=plaza")
        self.assertEqual(status, 201)
        created = json.loads(body.decode("utf-8"))
        self.assertEqual(created["name"], "plaza")
        self.assertEqual(created["id"], "plaza")
        plaza_id = created["id"]

        status, _, body = await _http_get(self.port, "/worlds/create?name=cafe")
        self.assertEqual(status, 201)
        cafe = json.loads(body.decode("utf-8"))
        self.assertEqual(cafe["id"], "cafe")

        status, _, body = await _http_get(self.port, "/worlds/create?name=cafe")
        self.assertEqual(status, 201)
        cafe2 = json.loads(body.decode("utf-8"))
        self.assertEqual(cafe2["id"], "cafe-2")

        status, _, body = await _http_get(self.port, f"/worlds/create?name={quote('大厅')}")
        self.assertEqual(status, 400)

        status, _, body = await _http_get(self.port, "/worlds")
        self.assertEqual(status, 200)
        worlds = {item["id"] for item in json.loads(body.decode("utf-8"))["worlds"]}
        self.assertEqual(worlds, {plaza_id, "cafe", "cafe-2"})

        from agents_world.client import WorldClient

        async with WorldClient(self._ws(plaza_id), member_id="alice") as alice:
            async with WorldClient(self._ws("cafe"), member_id="bob") as bob:
                await alice.join()
                await bob.join()
                await alice.wait_for(lambda m: m.get("type") == "snapshot")
                await bob.wait_for(lambda m: m.get("type") == "snapshot")
                await alice.speak("only plaza")
                heard = await alice.wait_for(
                    lambda m: m.get("type") == "event" and m.get("text") == "only plaza"
                )
                self.assertEqual(heard.get("kind"), "utterance")
                with self.assertRaises(TimeoutError):
                    await bob.wait_for(
                        lambda m: m.get("type") == "event" and m.get("text") == "only plaza",
                        timeout=0.4,
                    )

    async def test_ws_requires_world_path(self):
        from agents_world.client import WorldClient

        client = WorldClient(f"ws://127.0.0.1:{self.port}/", member_id="alice")
        with self.assertRaises(Exception):
            await client.connect()
        await client.close()

    async def test_dummy_style_roundtrip(self):
        await _http_get(self.port, "/worlds/create?name=hall")
        from agents_world.client import WorldClient

        async with WorldClient(self._ws("hall"), member_id="alice") as alice:
            async with WorldClient(self._ws("hall"), member_id="bob") as bob:
                self.assertEqual(alice.welcome.get("protocol_version"), PROTOCOL_VERSION)
                await alice.join()
                await bob.join()
                await alice.wait_for(lambda m: m.get("type") == "snapshot")
                await bob.wait_for(lambda m: m.get("type") == "snapshot")
                await alice.speak("hello from alice")
                heard = await bob.wait_for(
                    lambda m: m.get("type") == "event" and m.get("kind") == "utterance"
                )
                self.assertEqual(heard.get("text"), "hello from alice")
                self.assertNotIn("room_id", heard)

    async def test_malformed_sync_keeps_connection(self):
        await _http_get(self.port, "/worlds/create?name=hall")
        from agents_world.client import WorldClient

        async with WorldClient(self._ws("hall"), member_id="alice") as alice:
            await alice.join()
            await alice.wait_for(lambda m: m.get("type") == "snapshot")
            await alice._send({"type": "sync", "after_seq": "abc"})
            err = await alice.wait_for(lambda m: m.get("type") == "error")
            self.assertEqual(err.get("code"), "bad_payload")
            await alice.speak("still connected")
            echo = await alice.wait_for(
                lambda m: m.get("type") == "event" and m.get("text") == "still connected"
            )
            self.assertEqual(echo.get("kind"), "utterance")

    async def test_speak_requires_presence(self):
        await _http_get(self.port, "/worlds/create?name=hall")
        from agents_world.client import WorldClient

        async with WorldClient(self._ws("hall"), member_id="alice") as alice:
            await alice.speak("too soon")
            err = await alice.wait_for(lambda m: m.get("type") == "error")
            self.assertEqual(err.get("code"), "not_present")

    async def test_replaced_connection_is_closed(self):
        await _http_get(self.port, "/worlds/create?name=hall")
        from agents_world.client import WorldClient

        first = WorldClient(self._ws("hall"), member_id="bob")
        await first.connect()
        await first.join()
        await first.wait_for(lambda m: m.get("type") == "snapshot")
        second = WorldClient(self._ws("hall"), member_id="bob")
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

    async def test_http_serves_inhabitant_page(self):
        status, headers, body = await _http_get(self.port, "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("content-type", ""))
        html = body.decode("utf-8")
        blob = html
        for src in _asset_paths(html):
            asset_status, _, asset_body = await _http_get(self.port, src)
            self.assertEqual(asset_status, 200)
            blob += asset_body.decode("utf-8", "replace")
        self.assertIn("Invite", blob)
        self.assertIn("Dismiss", blob)
        self.assertIn("New world", blob)
        self.assertIn("Attach file", blob)
        self.assertIn("Type @ to mention someone", blob)
        self.assertIn("world-mention-menu", blob)

    async def test_http_serves_spoken_file(self):
        await _http_get(self.port, "/worlds/create?name=hall")
        from agents_world.client import WorldClient

        async with WorldClient(self._ws("hall"), member_id="alice") as alice:
            await alice.join()
            await alice.wait_for(lambda m: m.get("type") == "snapshot")
            await alice.speak(
                "file",
                attachments=[{"name": "note.txt", "mime": "text/plain", "data": b"venue-bytes"}],
            )
            echo = await alice.wait_for(
                lambda m: m.get("type") == "event" and m.get("kind") == "utterance"
            )
            file_id = echo["attachments"][0]["id"]
            self.assertEqual(echo["attachments"][0]["url"], f"/worlds/hall/files/{file_id}")
        status, headers, body = await _http_get(self.port, f"/worlds/hall/files/{file_id}")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"venue-bytes")

    async def test_http_neighbors_and_404(self):
        missing_status, _, _ = await _http_get(self.port, "/nope")
        status, headers, body = await _http_get(self.port, "/neighbors")
        self.assertEqual(missing_status, 404)
        self.assertEqual(status, 200)
        payload = json.loads(body.decode("utf-8"))
        self.assertIn("agents", payload)

    async def test_hub_reloads_existing_worlds(self):
        await _http_get(self.port, "/worlds/create?name=keep")
        await self.hub.stop()
        self.hub = WorldHub.create(host="127.0.0.1", port=0, data_root=str(self.root))
        self.port = await self.hub.start()
        status, _, body = await _http_get(self.port, "/worlds")
        ids = {item["id"] for item in json.loads(body.decode("utf-8"))["worlds"]}
        self.assertIn("keep", ids)


if __name__ == "__main__":
    unittest.main()
