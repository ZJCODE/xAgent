"""Control-app work routes: listing, controls, cancel, and strict authz."""
import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from xagent.core.runtime.host import RuntimeHost
from xagent.core.runtime.turns import TurnStore
from xagent.core.runtime.work_store import WorkStore


class FakeAgent:
    def __init__(self):
        self.turn_store = TurnStore()
        self.steers = []
        self.aborts = []

    async def steer(self, turn_id, content, *, channel=None, user_id=None, room_name=None):
        self.steers.append((turn_id, content, channel, user_id, room_name))
        return True

    def abort(self, turn_id=None, *, channel=None):
        self.aborts.append((turn_id, channel))
        return True


class WorkApiTests(unittest.IsolatedAsyncioTestCase):
    async def _host(self):
        host = RuntimeHost(Path(tempfile.mkdtemp()))
        host.agent = FakeAgent()
        host.admin = None
        host._ensure_work_runtime()
        return host

    async def _create_work(self, host, **overrides):
        fields = dict(channel="api", user_id="alice", room_name="", source="chat",
                      goal="g", completion_criteria="")
        fields.update(overrides)
        return await host.work_store.create(**fields)

    async def test_listing_and_fetch_are_scoped_to_channel_and_user(self):
        host = await self._host()
        own = await self._create_work(host)
        await self._create_work(host, user_id="bob")
        await self._create_work(host, channel="weixin")
        app = host._create_control_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            listed = await client.get("/work", params={"user_id": "alice", "channel": "api"})
            fetched = await client.get(f"/work/{own['id']}", params={"user_id": "alice", "channel": "api"})
            cross_user = await client.get(f"/work/{own['id']}", params={"user_id": "bob", "channel": "api"})
            cross_channel = await client.get(f"/work/{own['id']}", params={"user_id": "alice", "channel": "weixin"})
            missing_user = await client.get(f"/work/{own['id']}", params={"channel": "api"})
            unknown = await client.get("/work/nope", params={"user_id": "alice", "channel": "api"})
        self.assertEqual([work["id"] for work in listed.json()["work"]], [own["id"]])
        self.assertEqual(fetched.json()["id"], own["id"])
        self.assertEqual(cross_user.status_code, 404)
        self.assertEqual(cross_channel.status_code, 404)
        self.assertEqual(missing_user.status_code, 422)
        self.assertEqual(unknown.status_code, 404)

    async def test_supplement_messages_are_persisted_and_forwarded(self):
        host = await self._host()
        work = await self._create_work(host)
        # A converted-style work: running under its own turn, no runner channel.
        await host.work_store.update(work["id"], status="running", turn_ids=["turn-9"])
        app = host._create_control_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                f"/work/{work['id']}/messages",
                json={"user_id": "alice", "channel": "api", "content": "also update the docs"},
            )
            wrong_user = await client.post(
                f"/work/{work['id']}/messages",
                json={"user_id": "bob", "channel": "api", "content": "hijack"},
            )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["state"], "received")
        self.assertTrue(payload["control_id"])
        record = await host.work_store.get(work["id"])
        self.assertEqual(record["controls"][0]["content"], "also update the docs")
        self.assertEqual(record["controls"][0]["state"], "incorporated")
        self.assertEqual(host.agent.steers, [("turn-9", "also update the docs", "api", "alice", "")])
        self.assertEqual(wrong_user.status_code, 404)
        self.assertEqual(len(record["controls"]), 1)

    async def test_messages_on_terminal_work_are_rejected(self):
        host = await self._host()
        work = await self._create_work(host)
        await host.work_store.update(work["id"], status="completed")
        app = host._create_control_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                f"/work/{work['id']}/messages",
                json={"user_id": "alice", "channel": "api", "content": "late"},
            )
        self.assertEqual(response.status_code, 409)

    async def test_cancel_flips_active_work_to_stopping_and_aborts_its_turn(self):
        host = await self._host()
        work = await self._create_work(host)
        await host.work_store.update(work["id"], turn_ids=["turn-9"])
        done = await self._create_work(host)
        await host.work_store.update(done["id"], status="completed")
        app = host._create_control_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/work/{work['id']}/cancel",
                                         json={"user_id": "alice", "channel": "api"})
            terminal = await client.post(f"/work/{done['id']}/cancel",
                                         json={"user_id": "alice", "channel": "api"})
            wrong_user = await client.post(f"/work/{work['id']}/cancel",
                                           json={"user_id": "bob", "channel": "api"})
        self.assertEqual(response.json()["stopping"], True)
        self.assertEqual((await host.work_store.get(work["id"]))["status"], "stopping")
        self.assertEqual(host.agent.aborts, [("turn-9", "api")])
        self.assertEqual(terminal.json()["stopping"], False)
        self.assertEqual(wrong_user.status_code, 404)

    async def test_work_runtime_is_derived_from_turn_store_location(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            host = RuntimeHost(Path(tmpdir))
            host.agent = FakeAgent()
            host.agent.turn_store = TurnStore(Path(tmpdir) / ".runtime" / "turns.sqlite3")
            host.admin = None
            host._ensure_work_runtime()
            self.assertEqual(host.work_store.path, Path(tmpdir) / ".runtime" / "work.sqlite3")


if __name__ == "__main__":
    unittest.main()
