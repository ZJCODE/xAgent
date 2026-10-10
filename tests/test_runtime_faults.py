"""Cross-process ownership and transport fault boundaries."""
import asyncio
import select
import subprocess
import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from xagent.core.runtime.host import RuntimeHost
from xagent.core.runtime.turns import TurnStore
from xagent.components.message import MessageStorage
from xagent.core.runtime.ownership import RuntimeAlreadyRunning, RuntimeOwnership, runtime_is_active
from xagent.core.runtime import ScheduledDeliveryContext, scheduled_delivery_context
from xagent.integrations.api.adapter import ApiChannelAdapter
from tests import test_agent_inbox as inbox_test_helpers
from tests.test_agent_chat_flow import CapturingModelClient, InMemoryMessageStorage


_TRY_OWNER = """
import sys
from xagent.core.runtime.ownership import RuntimeAlreadyRunning, RuntimeOwnership
try:
    owner = RuntimeOwnership(sys.argv[1]).acquire()
except RuntimeAlreadyRunning:
    sys.exit(42)
owner.release()
"""

_HOLD_OWNER = """
import sys
from xagent.core.runtime.ownership import RuntimeOwnership
owner = RuntimeOwnership(sys.argv[1]).acquire()
print('owned', flush=True)
try:
    sys.stdin.readline()
finally:
    owner.release()
"""


class RuntimeProcessFaultTests(unittest.TestCase):
    def test_child_process_cannot_acquire_until_authoritative_owner_releases(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with RuntimeOwnership(root):
                result = subprocess.run([sys.executable, "-c", _TRY_OWNER, str(root / ".")],
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 42, result.stderr)
                self.assertTrue(runtime_is_active(root))
            result = subprocess.run([sys.executable, "-c", _TRY_OWNER, str(root)],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(runtime_is_active(root))

    def test_sqlite_read_write_scopes_close_connections_even_on_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            for store in (MessageStorage(str(Path(tmpdir) / "messages.sqlite3")),
                          TurnStore(Path(tmpdir) / "turns.sqlite3")):
                with store._connect() as connection:
                    self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)
                with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
                    connection.execute("SELECT 1")
                with self.assertRaisesRegex(ValueError, "read failure"):
                    with store._connect() as connection:
                        raise ValueError("read failure")
                with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
                    connection.execute("SELECT 1")


class RuntimeBootstrapFaultTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_deadline_retains_ownership_until_unresponsive_writer_finishes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            host = RuntimeHost(tmpdir)
            host.ownership.acquire()
            host.shutdown_timeout = 0.02
            release = asyncio.Event()
            async def writer():
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        pass
            async def drain():
                pass
            worker = asyncio.create_task(writer())
            await asyncio.sleep(0)
            host._tasks["writer"] = worker
            host._drain = drain
            try:
                await host._shutdown()
                host._release_ownership_when_idle()
                self.assertTrue(runtime_is_active(tmpdir))
                self.assertEqual(host.state, "stopping")
                with self.assertRaises(RuntimeAlreadyRunning):
                    RuntimeOwnership(tmpdir).acquire()
                release.set()
                await worker
                await asyncio.sleep(0)
                self.assertFalse(runtime_is_active(tmpdir))
                self.assertEqual(host.state, "stopped")
            finally:
                release.set()
                await worker
                host.ownership.release()

    async def test_external_owner_prevents_host_bootstrap_and_recovery(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            process = subprocess.Popen([sys.executable, "-c", _HOLD_OWNER, str(root)],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True)
            try:
                ready, _, _ = select.select([process.stdout], [], [], 5)
                self.assertTrue(ready, "child did not acquire runtime ownership")
                self.assertEqual(process.stdout.readline().strip(), "owned")
                host = RuntimeHost(root)
                with patch("xagent.interfaces.base.BaseAgentRunner") as bootstrap:
                    with self.assertRaises(RuntimeAlreadyRunning):
                        await host.run()
                bootstrap.assert_not_called()
                self.assertIsNone(host.agent)
                self.assertIsNone(host.scheduler)
                self.assertIsNone(host.control_app)
            finally:
                if process.poll() is None:
                    process.stdin.write("release\n")
                    process.stdin.flush()
                process.communicate(timeout=5)
            self.assertFalse(runtime_is_active(root))


class RuntimeTurnFaultTests(unittest.IsolatedAsyncioTestCase):
    def agent(self):
        return inbox_test_helpers.AgentInboxTests()._build_agent(InMemoryMessageStorage(), CapturingModelClient([]))

    async def test_public_http_response_identifies_persisted_turn(self):
        agent = self.agent()
        async def drive(**kwargs):
            yield {"type": "message_done", "phase": "final", "content": "received"}
            yield {"type": "done"}
        agent._drive_claimed_turn = drive
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            adapter = ApiChannelAdapter(agent, contacts_file=root / "contacts.json", tasks_dir=root / "tasks")
            app = FastAPI()
            adapter.register_routes(app)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post("/chat", json={"user_message": "hello", "user_id": "alice", "event_id": "source-1", "request_id": "request-1"})
                duplicate = await client.post("/chat", json={"user_message": "hello", "user_id": "alice", "event_id": "source-1", "request_id": "request-1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), duplicate.json())
        self.assertEqual(response.json()["reply"], "received")
        self.assertEqual(response.json()["event_id"], "source-1")
        self.assertEqual(response.json()["request_id"], "request-1")
        self.assertTrue(response.json()["turn_id"])
        self.assertEqual(len(await agent.turn_store.list_records()), 1)

    async def test_public_stop_requires_id_and_cannot_cancel_voice_turn(self):
        agent = self.agent()
        voice = agent.inbox.reserve_turn(channel="voice")
        api = agent.inbox.reserve_turn(channel="api")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            adapter = ApiChannelAdapter(agent, contacts_file=root / "contacts.json", tasks_dir=root / "tasks")
            app = FastAPI()
            adapter.register_routes(app)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                missing = await client.post("/chat/stop", json={})
                empty = await client.post("/chat/stop", json={"turn_id": ""})
                other = await client.post("/chat/stop", json={"turn_id": voice.turn_id})
                own = await client.post("/chat/stop", json={"turn_id": api.turn_id})
        self.assertEqual(missing.status_code, 422)
        self.assertEqual(empty.status_code, 422)
        self.assertEqual(other.json(), {"stopped": False})
        self.assertFalse(voice.cancelled.is_set())
        self.assertEqual(own.json(), {"stopped": True})
        self.assertTrue(api.cancelled.is_set())

    async def test_timeout_after_tool_start_emits_review_required(self):
        agent = self.agent()
        agent.inbox.run_timeout = 0.01
        async def drive(**kwargs):
            yield {"type": "tool_call", "tool": "external_action"}
            await asyncio.Event().wait()
        agent._drive_claimed_turn = drive
        events = [event async for event in agent.chat_events("run", user_id="alice", channel="api")]
        error = next(event for event in events if event["type"] == "error")
        self.assertTrue(error["needs_review"])
        records = await agent.turn_store.list_records()
        self.assertTrue(records[0]["needs_review"])
        self.assertFalse(agent.inbox.busy)

    async def test_validation_error_after_tool_start_emits_review_required(self):
        agent = self.agent()
        async def drive(**kwargs):
            yield {"type": "tool_call", "tool": "external_action"}
            raise ValueError("tool response could not be decoded")
        agent._drive_claimed_turn = drive
        events = [event async for event in agent.chat_events("run", user_id="alice", channel="api")]
        error = next(event for event in events if event["type"] == "error")
        self.assertTrue(error["needs_review"])
        self.assertTrue((await agent.turn_store.list_records())[0]["needs_review"])

    async def test_cleanup_failure_releases_shared_inbox_turn(self):
        agent = self.agent()
        class FailedCleanupIterator:
            def __init__(self):
                self.finished = False
            async def __anext__(self):
                if self.finished:
                    raise StopAsyncIteration
                self.finished = True
                return {"type": "done"}
            async def aclose(self):
                raise RuntimeError("cleanup failed")
        agent._drive_claimed_turn = lambda **kwargs: FailedCleanupIterator()
        with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
            _ = [event async for event in agent.chat_events("hello", user_id="alice", channel="api")]
        self.assertFalse(agent.inbox.busy)
        ticket = agent.inbox.reserve_turn(channel="feishu")
        await asyncio.wait_for(agent.inbox.acquire_turn(ticket), 0.1)
        agent.inbox.release_turn()

    async def test_scheduled_retry_reexecutes_new_attempt_but_deduplicates_same_attempt(self):
        agent = self.agent()
        calls = []
        async def drive(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                yield {"type": "error", "error": "first attempt failed"}
            else:
                yield {"type": "message_done", "phase": "final", "content": "retry succeeded"}
            yield {"type": "done"}
        agent._drive_claimed_turn = drive
        async def attempt(attempt_id):
            context = ScheduledDeliveryContext(channel="api", user_id="alice", target={"user_id": "alice"},
                metadata={"source": "scheduled_task", "run_id": "occurrence-1", "attempt_id": attempt_id})
            with scheduled_delivery_context(context):
                return [event async for event in agent.chat_events("check", user_id="alice", channel="api")]
        first = await attempt("attempt-1")
        duplicate = await attempt("attempt-1")
        retry = await attempt("attempt-2")
        self.assertEqual(len(calls), 2)
        self.assertTrue(any(event.get("error") == "first attempt failed" for event in first))
        self.assertFalse(any(event["type"] == "accepted" for event in duplicate))
        self.assertTrue(any(event.get("content") == "retry succeeded" for event in retry))
        self.assertEqual(len(await agent.turn_store.list_records()), 2)
