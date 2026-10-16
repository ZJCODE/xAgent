"""Isolation, admission, resource acknowledgement and session-only connections."""
import asyncio
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import HTTPException

from xagent.core.attention import AttentionLoop
from xagent.core.inbox import AgentInbox
from xagent.core.runtime.host import RuntimeHost
from xagent.core.runtime.channel_activity import ChannelActivity
from xagent.core.runtime.subconscious import SubconsciousLoop
from xagent.core.runtime.tasks import AsyncTaskScheduler, enqueue_scheduled_task, list_task_records
from xagent.integrations.weixin.state import WeixinCredentials, WeixinStateStore
from xagent.interfaces.cli.channels import channel_readiness, default_runtime_channels
from xagent.interfaces.cli.parser import build_parser


class FakeAdapter:
    instances = []
    fail = False

    def __init__(self, **kwargs):
        self.stopped = asyncio.Event()
        self.closed = False
        self.stop_calls = 0
        self.quiet = False
        self.instances.append(self)

    async def run(self):
        try:
            if self.fail:
                raise RuntimeError("handshake rejected")
            self.runtime_status("connected")
            await self.stopped.wait()
        finally:
            self.closed = True

    def quiesce(self):
        self.quiet = True

    async def stop(self):
        self.stop_calls += 1
        self.stopped.set()

    @property
    def resources_closed(self):
        return self.closed

    def _can_handle_scheduled_task(self, task):
        return True


class ChannelLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        WeixinStateStore(self.root).save_credentials(
            WeixinCredentials("test", "https://example.test", "test", "owner")
        )
        self.host = RuntimeHost(self.root)
        self.host.runner = SimpleNamespace(config={"channels": {
            "api": {"enabled": False}, "weixin": {"account_id": "test"},
            "voice": {},
        }}, tasks_dir=self.root / "tasks")
        self.host.agent = SimpleNamespace(inbox=AgentInbox(), attention=AttentionLoop(addressed_grace=0))
        self.host.api = SimpleNamespace(tasks=SimpleNamespace(can_handle=lambda task: True))
        self.host.adapters["api"] = self.host.api
        self.host.channel_status = {name: {"state": "disconnected"} for name in ("api", "weixin", "voice")}
        self.host.runtime_ready = True
        self.host.state = "running"
        self.host.channel_drain_timeout = 0.04
        self.host.channel_cleanup_timeout = 0.04
        self.host._publish_status = Mock()
        FakeAdapter.instances = []
        self.patch = patch("xagent.integrations.weixin.WeixinAdapter", FakeAdapter)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def asyncTearDown(self):
        for activity in self.host._channel_activity.values():
            for task in activity.tasks:
                task.cancel()
        for adapter in self.host.adapters.values():
            if isinstance(adapter, FakeAdapter):
                adapter.stopped.set()
        tasks = set(self.host._tasks.values()) | set(self.host._channel_operations.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.host.agent.attention.stop()

    async def connect(self):
        await self.host.connect_channel("weixin")
        await asyncio.sleep(0)

    async def test_concurrent_connect_is_one_instance_and_disconnect_is_idempotent(self):
        await asyncio.gather(*(self.host.connect_channel("weixin") for _ in range(3)))
        await asyncio.sleep(0)
        self.assertEqual(len(FakeAdapter.instances), 1)
        self.assertEqual(self.host.channel_status["weixin"]["state"], "connected")
        await asyncio.gather(*(self.host.disconnect_channel("weixin") for _ in range(3)))
        self.assertEqual(FakeAdapter.instances[0].stop_calls, 1)
        self.assertEqual(self.host.channel_status["weixin"]["state"], "disconnected")
        self.assertEqual(self.host.state, "running")
        self.assertTrue(self.host._channel_view("weixin")["can_connect"])
        self.assertIs(self.host.adapters["api"], self.host.api)

    async def test_reconnect_ignores_old_callbacks_and_does_not_change_config(self):
        before = repr(self.host.runner.config)
        await self.connect()
        old_callback = FakeAdapter.instances[-1].runtime_status
        await self.host.disconnect_channel("weixin")
        await self.connect()
        old_callback("failed")
        await asyncio.sleep(0)
        self.assertEqual(self.host.channel_status["weixin"]["state"], "connected")
        self.assertEqual(repr(self.host.runner.config), before)
        self.assertEqual(len(FakeAdapter.instances), 2)

    async def test_drain_finishes_accepted_work_before_closing(self):
        await self.connect()
        finished = []
        async def accepted():
            await asyncio.sleep(0.01)
            finished.append(not FakeAdapter.instances[0].closed)
        self.host._channel_activity["weixin"].create_task(accepted())
        await self.host.disconnect_channel("weixin")
        self.assertEqual(finished, [True])

    async def test_timeout_cancels_only_owned_work_and_queue(self):
        await self.connect()
        inbox = self.host.agent.inbox
        own = inbox.reserve_turn(channel="weixin")
        other = inbox.reserve_turn(channel="api")
        work = self.host._channel_activity["weixin"].create_task(asyncio.Event().wait())
        unrelated = asyncio.create_task(asyncio.Event().wait())
        try:
            await self.host.disconnect_channel("weixin")
            self.assertTrue(work.cancelled())
            self.assertTrue(own.cancelled.is_set())
            self.assertFalse(other.cancelled.is_set())
            self.assertFalse(unrelated.done())
            self.assertTrue(inbox.accepting)
        finally:
            unrelated.cancel()
            await asyncio.gather(unrelated, return_exceptions=True)

    async def test_cleanup_timeout_retains_instance_until_acknowledged(self):
        await self.connect()
        adapter = FakeAdapter.instances[0]
        release = asyncio.Event()
        async def slow_stop():
            await release.wait()
            adapter.stopped.set()
        adapter.stop = slow_stop
        with self.assertRaises(HTTPException) as error:
            await self.host.disconnect_channel("weixin")
        self.assertEqual(error.exception.status_code, 503)
        self.assertFalse(self.host._channel_view("weixin")["can_connect"])
        self.assertTrue(self.host._channel_view("weixin")["can_disconnect"])
        with self.assertRaises(HTTPException) as conflict:
            await self.host.connect_channel("weixin")
        self.assertEqual(conflict.exception.status_code, 409)
        release.set()
        await self.host.disconnect_channel("weixin")
        self.assertNotIn("weixin", self.host.adapters)

    async def test_disconnect_survives_caller_cancellation(self):
        await self.connect()
        self.host._channel_activity["weixin"].create_task(asyncio.sleep(0.02))
        caller = asyncio.create_task(self.host.disconnect_channel("weixin"))
        await asyncio.sleep(0)
        caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
        await self.host._channel_operations["weixin"]
        self.assertEqual(self.host.channel_status["weixin"]["state"], "disconnected")

    async def test_connecting_and_reconnecting_can_be_disconnected(self):
        for state in ("connecting", "reconnecting"):
            await self.connect()
            self.host._update_channel_state("weixin", state)
            self.assertTrue(self.host._channel_view("weixin")["can_disconnect"])
            await self.host.disconnect_channel("weixin")
            self.assertEqual(self.host.state, "running")

    async def test_failed_transport_is_cleaned_and_can_retry(self):
        class RejectedAdapter(FakeAdapter):
            fail = True
        with patch("xagent.integrations.weixin.WeixinAdapter", RejectedAdapter):
            await self.host.connect_channel("weixin")
            await asyncio.sleep(0)
            operation = self.host._channel_operations["weixin"]
            await operation
        self.assertEqual(self.host.channel_status["weixin"]["state"], "failed")
        self.assertTrue(self.host._channel_view("weixin")["can_connect"])
        await self.connect()
        self.assertEqual(self.host.state, "running")

    async def test_control_validation_and_shutdown_admission(self):
        for name, code in (("unknown", 404), ("voice", 422)):
            with self.assertRaises(HTTPException) as error:
                await self.host.connect_channel(name)
            self.assertEqual(error.exception.status_code, code)
        self.host._stop_event.set()
        with self.assertRaises(HTTPException) as error:
            await self.host.connect_channel("weixin")
        self.assertEqual(error.exception.status_code, 409)

    async def test_restart_stop_captures_intent_atomically_and_reuses_first_snapshot(self):
        self.host.channel_status = {
            "api": {"state": "connected"}, "weixin": {"state": "connecting"},
            "voice": {"state": "disconnecting"},
        }
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.host._create_control_app()), base_url="http://test") as client:
            first = await client.post("/runtime/stop", json={"restart": True})
            self.assertEqual(first.json()["restart_channels"], ["api", "weixin"])
            self.assertTrue(self.host._stop_event.is_set())
            self.host.channel_status["weixin"] = {"state": "failed"}
            second = await client.post("/runtime/stop")
            self.assertEqual(second.json(), first.json())
            self.assertTrue(self.host._restarting)

    async def test_restart_snapshot_excludes_queued_disconnect(self):
        self.host.channel_status["weixin"] = {"state": "connected"}
        pending = asyncio.create_task(asyncio.Event().wait())
        self.host._channel_operations["weixin"] = pending
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.host._create_control_app()), base_url="http://test") as client:
            response = await client.post("/runtime/stop", json={"restart": True})
        self.assertEqual(response.json()["restart_channels"], [])

    async def test_ordinary_stop_does_not_record_or_upgrade_to_restart_intent(self):
        self.host.channel_status["weixin"] = {"state": "connected"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.host._create_control_app()), base_url="http://test") as client:
            first = await client.post("/runtime/stop")
            second = await client.post("/runtime/stop", json={"restart": True})
        self.assertEqual(first.json(), {"stopping": True})
        self.assertEqual(second.json(), first.json())
        self.assertFalse(self.host._restarting)

    async def test_internal_routes_use_same_lifecycle(self):
        self.host.detailed_status = AsyncMock(return_value={})
        app = self.host._create_control_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            result = await client.post("/runtime/channels/weixin/connect")
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["channel"]["id"], "weixin")
            result = await client.post("/runtime/channels/weixin/disconnect")
            self.assertEqual(result.json()["channel"]["state"], "disconnected")
            self.assertEqual((await client.post("/runtime/channels/other/connect")).status_code, 404)

    async def test_scheduler_defers_disconnected_channel_and_wakes_on_connect(self):
        task = enqueue_scheduled_task(task_type="message", content="test",
            run_at=datetime.now() - timedelta(seconds=1), tasks_dir=self.host.runner.tasks_dir,
            channel="weixin", target={"user_id": "test"})
        delivered = []
        async def dispatch(record):
            delivered.append(record.task_id)
        scheduler = AsyncTaskScheduler(self.host.runner.tasks_dir,
            can_handle=self.host._can_handle_task, dispatch=dispatch)
        self.host.scheduler = scheduler
        await scheduler.tick()
        self.assertEqual(delivered, [])
        await self.connect()
        self.assertTrue(scheduler._wake_event.is_set())
        await scheduler.tick()
        self.assertEqual(delivered, [task.task_id])
        await self.host.disconnect_channel("weixin")
        await self.connect()
        await scheduler.tick()
        self.assertEqual(delivered, [task.task_id])

    async def test_attention_cancel_does_not_stop_other_channels_or_replay(self):
        await self.connect()
        attention = self.host.agent.attention
        started = asyncio.Event()
        replies = []
        async def slow_speak(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        async def other_speak(*args, **kwargs):
            replies.append("api")
        attention.register_speaker("weixin", slow_speak)
        attention.register_speaker("api", other_speak)
        await attention.notice("weixin:room", addressed=True, content="old")
        await started.wait()
        await attention.notice("api:room", addressed=True, content="keep working")
        await self.host.disconnect_channel("weixin")
        await attention.idle()
        self.assertEqual(replies, ["api"])
        self.assertFalse(attention._task.done())
        self.assertEqual(attention.cursors("weixin:room").events, [])
        await self.connect()
        await attention.idle()
        self.assertEqual(replies, ["api"])

    async def test_active_scheduled_task_is_reviewed_after_disconnect_timeout(self):
        await self.connect()
        started = asyncio.Event()
        async def execute(record):
            started.set()
            await asyncio.Event().wait()
        scheduler = AsyncTaskScheduler(self.host.runner.tasks_dir,
            can_handle=self.host._can_handle_task, execute=execute, deliver=AsyncMock())
        self.host.scheduler = scheduler
        enqueue_scheduled_task(task_type="agent", content="test", run_at=datetime.now() - timedelta(seconds=1),
            tasks_dir=self.host.runner.tasks_dir, channel="weixin", target={"user_id": "test"})
        await scheduler.tick(wait_for_dispatches=False)
        await started.wait()
        await self.host.disconnect_channel("weixin")
        records = list_task_records(self.host.runner.tasks_dir, include_running=True)
        self.assertEqual(records[0].status, "needs_review")
        await self.connect()
        await scheduler.tick()
        self.assertEqual(scheduler.status_snapshot()["needs_review"], 1)

    async def test_subconscious_rechecks_state_and_updates_recipients(self):
        loop = SubconsciousLoop(self.host.agent, workspace=self.root, deliverable_channels={"api"})
        self.host.heartbeat = SimpleNamespace(subconscious_loop=loop)
        await self.connect()
        self.assertIn("weixin", loop._deliverable_channels)
        adapter = FakeAdapter.instances[0]
        adapter.deliver_subconscious_message = AsyncMock()
        delivery = SimpleNamespace(recipient=SimpleNamespace(channel="weixin"))
        await self.host._deliver_subconscious(delivery)
        await self.host.disconnect_channel("weixin")
        self.assertNotIn("weixin", loop._deliverable_channels)
        self.assertIn("api", loop._deliverable_channels)
        with self.assertRaises(RuntimeError):
            await self.host._deliver_subconscious(delivery)
        adapter.deliver_subconscious_message.assert_awaited_once()


class ChannelDefaultsTests(unittest.TestCase):
    def test_external_enabled_never_changes_default(self):
        config = {"channels": {name: {"enabled": True} for name in ("api", "voice", "weixin")}}
        self.assertEqual(default_runtime_channels(config), ["api"])
        config["channels"]["api"]["enabled"] = False
        self.assertEqual(default_runtime_channels(config), [])
        self.assertEqual(default_runtime_channels({}), ["api"])

    def test_readiness_does_not_create_weixin_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ready, _ = channel_readiness({"channels": {"weixin": {"account_id": "test"}}}, "weixin", root)
            self.assertFalse(ready)
            self.assertFalse((root / "weixin").exists())

    def test_cli_has_session_commands_and_explicit_start_override(self):
        parser = build_parser()
        args = parser.parse_args(["channel", "voice", "connect", "--agent", "atlas"])
        self.assertEqual((args.channel, args.channel_action, args.agent), ("voice", "connect", "atlas"))
        self.assertIsNone(parser.parse_args(["start"]).channels)
        self.assertEqual(parser.parse_args(["restart", "--channels", "api,weixin"]).channels, ["api,weixin"])


class AdapterResourceTests(unittest.IsolatedAsyncioTestCase):
    def voice(self, recognizer):
        from tests.test_voice_runtime import FakeMicrophone, FakePlayer, FakeSynthesizer, FakeAgent, voice_config
        from xagent.interfaces.voice.runtime import VoiceRuntime
        return VoiceRuntime(agent=FakeAgent(), config=voice_config(), microphone=FakeMicrophone(),
            recognizer=recognizer, synthesizer=FakeSynthesizer(), player=FakePlayer(), output=lambda *args: None)

    async def test_voice_capture_failure_propagates_and_closes(self):
        class BrokenRecognizer:
            def iter_utterances(self, *args, **kwargs):
                raise RuntimeError("capture failed")
                yield
        runtime = self.voice(BrokenRecognizer())
        states = []
        runtime.runtime_status = states.append
        with self.assertRaisesRegex(RuntimeError, "capture failed"):
            await asyncio.wait_for(runtime.run_forever(), timeout=1)
        self.assertEqual(states, [])
        self.assertTrue(runtime.resources_closed)

    async def test_voice_quiesce_allows_accepted_reply_to_finish(self):
        from tests.test_voice_runtime import FakeRecognizer
        from xagent.interfaces.voice.types import VoiceUtterance
        runtime = self.voice(FakeRecognizer([VoiceUtterance(text="hello")]))
        runtime.runtime_activity = ChannelActivity()
        started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def reply(utterance):
            started.set()
            await release.wait()
            finished.set()
        runtime._handle_utterance = reply
        running = asyncio.create_task(runtime.run_forever())
        try:
            await started.wait()
            runtime.runtime_activity.quiesce()
            runtime.quiesce()
            self.assertFalse(runtime.synthesizer.cancelled)
            release.set()
            await finished.wait()
            self.assertFalse(running.done())
            await runtime.stop()
            await asyncio.wait_for(running, timeout=1)
            self.assertTrue(runtime.resources_closed)
        finally:
            release.set()
            await runtime.stop()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    async def test_cancelled_voice_wrapper_keeps_device_worker_tracked(self):
        from tests.test_voice_runtime import FakeRecognizer
        runtime = self.voice(FakeRecognizer([]))
        started, release = threading.Event(), threading.Event()
        def device():
            started.set()
            release.wait()
        wrapper = asyncio.create_task(runtime._resource_call(device))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            wrapper.cancel()
            await asyncio.gather(wrapper, return_exceptions=True)
            self.assertTrue(runtime._resource_workers)
            release.set()
            await asyncio.gather(*runtime._resource_workers)
            self.assertFalse(runtime._resource_workers)
        finally:
            release.set()
