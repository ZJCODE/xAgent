"""Authority, private transport, queue admission, and restart receipts."""
import asyncio
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
import httpx

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
        second = asyncio.create_task(collect("second", "weixin"))
        for _ in range(100):
            if len(agent.message_storage.messages) == 2:
                break
            await asyncio.sleep(0.001)
        self.assertEqual([row.content for row in agent.message_storage.messages], ["first", "second"])
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(agent.inbox.pending_count, 1)
        self.assertFalse(agent.abort(channel="weixin"))
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
        ticket = inbox.reserve_turn(channel="weixin")
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
    async def _start_host(self, root, model, *, channels=None):
        def bootstrap(config_dir):
            runner = BaseAgentRunner(config_dir=config_dir)
            runner.agent.model_client = model
            runner.agent.memory_handler = FakeMemoryHandler()
            return runner
        host = RuntimeHost(root, channels=channels)
        with patch("xagent.interfaces.base.BaseAgentRunner", side_effect=bootstrap):
            task = asyncio.create_task(host.run())
            for _ in range(300):
                if host.runtime_ready or task.done():
                    break
                await asyncio.sleep(0.01)
            if task.done():
                await task
        self.assertTrue(host.runtime_ready)
        return host, task

    async def test_default_start_and_restart_leave_external_channels_disconnected(self):
        from tests.test_channel_lifecycle import FakeAdapter
        from xagent.integrations.weixin.state import WeixinCredentials, WeixinStateStore
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(root)
            WeixinStateStore(root).save_credentials(WeixinCredentials("test", "https://example.test", "test", "owner"))
            config = yaml.safe_load((root / "config.yaml").read_text())
            with socket.socket() as available:
                available.bind(("127.0.0.1", 0))
                port = available.getsockname()[1]
            config["channels"]["api"] = {"enabled": True, "host": "127.0.0.1", "port": port}
            config["channels"].update({
                "voice": {"enabled": True, "api_key": "test-key"},
                "weixin": {"enabled": True, "account_id": "test"},
            })
            (root / "config.yaml").write_text(yaml.safe_dump(config))
            with patch("xagent.integrations.weixin.WeixinAdapter", FakeAdapter), patch("xagent.interfaces.voice.factory.create_websocket_voice_runtime") as voice:
                first, task = await self._start_host(root, CapturingModelClient([]))
                try:
                    self.assertTrue(all(row["state"] == "disconnected" for name, row in first.channel_status.items() if name != "api"))
                    self.assertIn("api", first._servers)
                    await first.connect_channel("weixin")
                    self.assertIn("weixin", first.adapters)
                finally:
                    await first.stop()
                    await asyncio.wait_for(task, timeout=5)
                second, task = await self._start_host(root, CapturingModelClient([]))
                try:
                    self.assertTrue(all(row["state"] == "disconnected" for name, row in second.channel_status.items() if name != "api"))
                    self.assertIn("api", second._servers)
                    self.assertNotIn("weixin", second.adapters)
                    voice.assert_not_called()
                finally:
                    await second.stop()
                    await asyncio.wait_for(task, timeout=5)

    async def test_explicit_start_override_connects_requested_channel(self):
        from tests.test_channel_lifecycle import FakeAdapter
        from xagent.integrations.weixin.state import WeixinCredentials, WeixinStateStore
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(root)
            WeixinStateStore(root).save_credentials(WeixinCredentials("test", "https://example.test", "test", "owner"))
            config = yaml.safe_load((root / "config.yaml").read_text())
            config["channels"]["weixin"] = {"enabled": False, "account_id": "test"}
            (root / "config.yaml").write_text(yaml.safe_dump(config))
            with patch("xagent.integrations.weixin.WeixinAdapter", FakeAdapter):
                host, task = await self._start_host(root, CapturingModelClient([]), channels=["weixin"])
                try:
                    await asyncio.sleep(0)
                    self.assertEqual(host.channel_status["weixin"]["state"], "connected")
                    self.assertEqual(host.channel_status["api"]["state"], "disconnected")
                finally:
                    await host.stop()
                    await asyncio.wait_for(task, timeout=5)

    async def test_shared_restart_restores_connections_and_keeps_empty_selection(self):
        from tests.test_channel_lifecycle import FakeAdapter
        from xagent.interfaces.cli.agent_runtime import restart_runtime
        from xagent.integrations.weixin.state import WeixinCredentials, WeixinStateStore

        class FakeVoice(FakeAdapter):
            run_forever = FakeAdapter.run

        for restore in (True, False):
            with self.subTest(restore=restore), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                write_config(root)
                config = yaml.safe_load((root / "config.yaml").read_text())
                with socket.socket() as available:
                    available.bind(("127.0.0.1", 0))
                    port = available.getsockname()[1]
                config["channels"] = {
                    "api": {"enabled": True, "host": "127.0.0.1", "port": port},
                    "weixin": {"account_id": "test"}, "voice": {"api_key": "test-key"},
                }
                (root / "config.yaml").write_text(yaml.safe_dump(config))
                store = WeixinStateStore(root)
                store.save_credentials(WeixinCredentials("test", "https://example.test", "test", "owner"))
                loop = asyncio.get_running_loop()
                hosts = []
                selected = ["api", "weixin"] if restore else []

                def launch(root, channels):
                    self.assertEqual(channels, selected)
                    future = asyncio.run_coroutine_threadsafe(self._start_host(root, CapturingModelClient([]), channels=channels), loop)
                    host, task = future.result(timeout=5)
                    hosts.append((host, task))
                    return host.status()

                with patch(
                    "xagent.integrations.weixin.WeixinAdapter", FakeAdapter,
                ), patch(
                    "xagent.interfaces.cli.agent_runtime.start_runtime", side_effect=launch,
                ):
                    first, task = await self._start_host(root, CapturingModelClient([]), channels=selected)
                    hosts.append((first, task))
                    try:
                        for _ in range(100):
                            if all(first.channel_status[name]["state"] == "connected" for name in selected):
                                break
                            await asyncio.sleep(0.01)
                        result = await restart_runtime(root)
                        self.assertTrue(task.done())
                        await task
                        second = hosts[-1][0]
                        for _ in range(100):
                            if all(second.channel_status[name]["state"] == "connected" for name in selected):
                                break
                            await asyncio.sleep(0.01)
                        connected = [name for name, row in second.channel_status.items() if row["state"] == "connected"]
                        self.assertEqual(connected, selected)
                        self.assertNotIn("restart_warnings", result)
                        if not restore:
                            self.assertNotIn("api", second._servers)
                    finally:
                        for host, running in reversed(hosts):
                            await host.stop()
                            await asyncio.wait_for(running, timeout=5)

    async def test_terminal_channel_controls_can_reconnect_using_live_capabilities(self):
        from tests.test_channel_lifecycle import FakeAdapter
        from xagent.integrations.weixin.state import WeixinCredentials, WeixinStateStore
        from xagent.interfaces.cli.launcher import _managed_channel_actions, _run_managed_channel_action

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(root)
            WeixinStateStore(root).save_credentials(WeixinCredentials("test", "https://example.test", "test", "owner"))
            config = yaml.safe_load((root / "config.yaml").read_text())
            config["channels"]["weixin"] = {"account_id": "test"}
            (root / "config.yaml").write_text(yaml.safe_dump(config))
            with patch("xagent.integrations.weixin.WeixinAdapter", FakeAdapter):
                host, task = await self._start_host(root, CapturingModelClient([]))
                try:
                    for _ in range(2):
                        actions = await asyncio.to_thread(_managed_channel_actions, root, "weixin")
                        self.assertFalse(next(row for row in actions if row.key == "connect").disabled)
                        self.assertEqual(await asyncio.to_thread(
                            _run_managed_channel_action, root, "weixin", "connect"), 0)
                        actions = await asyncio.to_thread(_managed_channel_actions, root, "weixin")
                        self.assertFalse(next(row for row in actions if row.key == "disconnect").disabled)
                        self.assertEqual(await asyncio.to_thread(
                            _run_managed_channel_action, root, "weixin", "disconnect"), 0)
                        self.assertTrue(host.runtime_ready)
                        self.assertNotIn("weixin", host.adapters)
                    actions = await asyncio.to_thread(_managed_channel_actions, root, "weixin")
                    self.assertFalse(next(row for row in actions if row.key == "connect").disabled)
                finally:
                    await host.stop()
                    await asyncio.wait_for(task, timeout=5)

    async def test_public_api_disconnect_preserves_active_local_turn_and_notifications(self):
        started = asyncio.Event()
        release = asyncio.Event()
        class BlockingModel(CapturingModelClient):
            async def model_turn_events(self, **kwargs):
                started.set()
                await release.wait()
                yield type("E", (), {"type": "text", "delta": "local reply", "tool_calls": None, "error": None})()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(root)
            with socket.socket() as available:
                available.bind(("127.0.0.1", 0))
                port = available.getsockname()[1]
            config = yaml.safe_load((root / "config.yaml").read_text())
            config["channels"]["api"]["port"] = port
            (root / "config.yaml").write_text(yaml.safe_dump(config))
            host, host_task = await self._start_host(root, BlockingModel([]))
            client = RuntimeClient(root)
            local_turn = None
            try:
                async def chat():
                    return [event async for event in client.chat_events(user_id="alice", user_message="hello", channel="api")]
                local_turn = asyncio.create_task(chat())
                await started.wait()
                await client.connect_channel("api")
                for _ in range(200):
                    if host.channel_status["api"]["state"] == "connected":
                        break
                    await asyncio.sleep(0.01)
                async with httpx.AsyncClient(trust_env=False) as public:
                    self.assertEqual((await public.get(f"http://127.0.0.1:{port}/health")).status_code, 200)
                await client.disconnect_channel("api")
                self.assertFalse(local_turn.done())
                self.assertIs(host.adapters["api"], host.api)
                self.assertTrue((await client.status())["runtime_ready"])
                release.set()
                events = await local_turn
                self.assertTrue(any(event.get("content") == "local reply" for event in events))
                notifications = []
                class Subscriber:
                    async def send_json(self, event):
                        notifications.append(event)
                subscriber = Subscriber()
                await host.api.delivery.register_subscriber("alice", subscriber)
                from xagent.core.runtime import enqueue_scheduled_task
                record = enqueue_scheduled_task(task_type="message", content="local notification",
                    run_at="2099-01-01 00:00:00", tasks_dir=host.runner.tasks_dir,
                    channel="api", target={"user_id": "alice"})
                await host.api.tasks.deliver(record, await host.api.tasks.execute(record), record.task_id)
                self.assertTrue(notifications)
                await host.api.delivery.unregister_subscriber("alice", subscriber)
                await client.connect_channel("api")
                await client.disconnect_channel("api")
                for _ in range(3):
                    # Direct calls expose startup/bind races without HTTP latency.
                    await host.connect_channel("api")
                    await host.disconnect_channel("api")
                    with socket.socket() as available:
                        available.bind(("127.0.0.1", port))
            finally:
                release.set()
                if local_turn is not None:
                    await asyncio.gather(local_turn, return_exceptions=True)
                await host.stop()
                await asyncio.wait_for(host_task, timeout=5)

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
                    self.assertEqual(set(status["channels"]), {"api", "weixin"})
                    self.assertTrue(all(row["state"] == "disconnected" for row in status["channels"].values()))
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

    async def test_subconscious_contact_deliverable_validates_weixin_context_token(self):
        from xagent.core.runtime.subconscious import ContactEntry

        with tempfile.TemporaryDirectory() as tmp:
            host = RuntimeHost(tmp)
            fake_wx_adapter = type(
                "FakeWxAdapter",
                (),
                {"_context_tokens": {"wx_ok": "tok_123"}},
            )()
            host.adapters["weixin"] = fake_wx_adapter
            host.channel_status["weixin"] = {"state": "connected", "ready": True, "error": None}

            ok_contact = ContactEntry(
                channel="weixin",
                user_id="wx_ok",
                target={"user_id": "wx_ok"},
                last_seen="2026-08-16 00:00:00",
            )
            missing_token_contact = ContactEntry(
                channel="weixin",
                user_id="wx_no_tok",
                target={"user_id": "wx_no_tok"},
                last_seen="2026-08-16 00:00:00",
            )
            self.assertTrue(host._is_subconscious_contact_deliverable(ok_contact))
            self.assertFalse(host._is_subconscious_contact_deliverable(missing_token_contact))

