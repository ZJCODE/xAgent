"""Authority, private transport, queue admission, and restart receipts."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from xagent.components import MessageStorage
from xagent.core.agent import Agent
from xagent.core.config import ReplyType
from xagent.core.inbox import AgentInbox, InboxCapacityError, InboxQueueTimeout
from xagent.core.runtime.client import RuntimeClient
from xagent.core.runtime.host import RuntimeHost
from xagent.interfaces.server.admin_service import AdminService
from xagent.core.runtime.ownership import RuntimeAlreadyRunning, RuntimeOwnership, runtime_is_active, runtime_owned_here, runtime_paths
from xagent.core.runtime.turns import TurnStore
from xagent.interfaces.base import BaseAgentRunner
from tests.test_agent_inbox import AgentInboxTests
from tests.test_agent_chat_flow import CapturingModelClient, FakeMemoryHandler, InMemoryMessageStorage


def write_config(root):
    (root / "config.yaml").write_text(yaml.safe_dump({
        "provider": {"name": "openai", "model": "test-model", "api_key": "test-key"},
        "channels": {"api": {"enabled": False}},
        "runtime": {"heartbeat_enabled": False},
    }), encoding="utf-8")
    (root / "identity.md").write_text("I am a test agent.", encoding="utf-8")


class RuntimeOwnershipTests(unittest.TestCase):
    def test_read_only_probe_does_not_create_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "missing"
            self.assertFalse(runtime_is_active(root))
            self.assertFalse(root.exists())

    def test_canonical_path_excludes_second_owner_and_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            owner = RuntimeOwnership(root).acquire()
            try:
                self.assertTrue(runtime_is_active(root))
                self.assertTrue(runtime_owned_here(root))
                with self.assertRaises(RuntimeAlreadyRunning):
                    RuntimeOwnership(root / ".." / root.name).acquire()
                self.assertEqual(runtime_paths(root).socket_path, runtime_paths(root / ".").socket_path)
            finally:
                owner.release()
            self.assertFalse(runtime_is_active(root))
            self.assertFalse(runtime_owned_here(root))


class AgentAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def build_agent(self, model):
        return AgentInboxTests()._build_agent(InMemoryMessageStorage(), model)

    async def test_accepted_queue_input_is_persisted_before_prior_turn_finishes(self):
        started = asyncio.Event()
        release = asyncio.Event()
        class BlockingModel(CapturingModelClient):
            async def model_turn_events(self, **kwargs):
                self.calls.append(kwargs)
                if len(self.calls) == 1:
                    started.set()
                    await release.wait()
                yield type("E", (), {"type": "text", "delta": "ok", "tool_calls": None, "error": None})()
        model = BlockingModel([])
        agent = self.build_agent(model)
        async def collect(text, channel):
            return [event async for event in agent.chat_events(text, user_id="alice", channel=channel)]
        first = asyncio.create_task(collect("first", "api"))
        await started.wait()
        second = asyncio.create_task(collect("second", "feishu"))
        for _ in range(100):
            if len(agent.message_storage.messages) == 2:
                break
            await asyncio.sleep(0.001)
        self.assertEqual([row.content for row in agent.message_storage.messages], ["first", "second"])
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(agent.inbox.pending_count, 1)
        self.assertFalse(agent.abort(channel="feishu"))
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(len(model.calls), 2)

    async def test_duplicate_event_replays_without_model_or_input_duplication(self):
        model = CapturingModelClient([(ReplyType.SIMPLE_REPLY, "ok")])
        agent = self.build_agent(model)
        first = [event async for event in agent.chat_events("hello", user_id="alice", channel="api", event_id="request-1")]
        second = [event async for event in agent.chat_events("hello", user_id="alice", channel="api", event_id="request-1")]
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(len(agent.message_storage.messages), 2)
        self.assertEqual(first[-1], second[-1])

    async def test_queue_has_one_shared_capacity_and_timeout(self):
        inbox = AgentInbox(max_pending=1, queue_timeout=0.02)
        await inbox.acquire_turn()
        ticket = inbox.reserve_turn(channel="feishu")
        with self.assertRaises(InboxCapacityError):
            inbox.reserve_turn(channel="voice")
        with self.assertRaises(InboxQueueTimeout):
            await inbox.acquire_turn(ticket)
        self.assertEqual(inbox.pending_count, 0)
        inbox.release_turn()

    async def test_restart_records_interrupted_without_reexecution(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "turns.sqlite3"
            store = TurnStore(path)
            record, fresh = await store.begin(event_key="inbound", channel="api")
            self.assertTrue(fresh)
            await store.update(record["turn_id"], state="running")
            reopened = TurnStore(path)
            self.assertEqual(await reopened.recover(), 1)
            record, fresh = await reopened.begin(event_key="inbound")
            self.assertFalse(fresh)
            self.assertEqual(record["state"], "interrupted")
            self.assertTrue(record["needs_review"])

    async def test_input_dedupe_is_atomic_and_delivery_rows_are_distinct(self):
        from xagent.schemas import Message, RoleType
        with tempfile.TemporaryDirectory() as tmp:
            storage = MessageStorage(str(Path(tmp) / "messages.sqlite3"))
            message = Message.create("hello", sender_id="alice")
            message.metadata.update(event_id="same", event_scope="api:alice")
            rows = await asyncio.gather(storage.add_messages(message), storage.add_messages(message))
            self.assertEqual(rows[0][0].metadata["storage_cursor"], rows[1][0].metadata["storage_cursor"])
            reply = Message.create("ok", role=RoleType.ASSISTANT)
            deliveries = await asyncio.gather(storage.add_message_once(reply, idempotency_key="run-1"), storage.add_message_once(reply, idempotency_key="run-1"))
            self.assertEqual(deliveries[0].metadata["storage_cursor"], deliveries[1].metadata["storage_cursor"])
            self.assertEqual(len(await storage.get_messages()), 2)


class RuntimeTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_client_works_without_public_api_and_owner_precedes_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(root)
            model = CapturingModelClient([(ReplyType.SIMPLE_REPLY, "hello")])
            def bootstrap(config_dir):
                self.assertTrue(runtime_owned_here(config_dir))
                runner = BaseAgentRunner(config_dir=config_dir)
                runner.agent.model_client = model
                runner.agent.memory_handler = FakeMemoryHandler()
                return runner
            host = RuntimeHost(root)
            with patch("xagent.interfaces.base.BaseAgentRunner", side_effect=bootstrap):
                task = asyncio.create_task(host.run())
                for _ in range(300):
                    if host.runtime_ready or task.done():
                        break
                    await asyncio.sleep(0.01)
                if task.done():
                    await task
                self.assertTrue(host.runtime_ready)
                client = RuntimeClient(root)
                try:
                    status = await client.status()
                    self.assertEqual(status["state"], "running")
                    self.assertEqual(status["channels"], {})
                    self.assertEqual(os.stat(host.paths.socket_path).st_mode & 0o777, 0o600)
                    events = [event async for event in client.chat_events(user_id="alice", user_message="hello", event_id="one")]
                    self.assertEqual(events[0]["type"], "accepted")
                    self.assertTrue(any(event.get("content") == "hello" for event in events))
                    (root / "identity.md").write_text("changed", encoding="utf-8")
                    self.assertTrue((await client.status())["needs_restart"])
                    await client.stop()
                finally:
                    await host.stop()
                    await asyncio.wait_for(task, timeout=5)
                self.assertFalse(runtime_is_active(root))
                self.assertFalse(host.paths.socket_path.exists())

    async def test_second_host_never_bootstraps_under_existing_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            owner = RuntimeOwnership(tmp).acquire()
            try:
                with patch("xagent.interfaces.base.BaseAgentRunner") as bootstrap:
                    with self.assertRaises(RuntimeAlreadyRunning):
                        await RuntimeHost(tmp).run()
                    bootstrap.assert_not_called()
            finally:
                owner.release()
