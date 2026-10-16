"""Restart must not wait for idle subscriptions or new model work."""
import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from xagent.core.runtime.host import RuntimeHost
from xagent.core.attention import AttentionLoop
from xagent.core.runtime.channel_activity import ChannelActivity
from xagent.interfaces.web.proxy import _local_websocket
from xagent.interfaces.cli.processes import start_background


class SubscriptionShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_event_stream_wakes_immediately_when_stopping(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = RuntimeHost(tmp)
            subscribed = asyncio.Event()
            async def register(*args):
                subscribed.set()
            delivery = SimpleNamespace(register_subscriber=register, unregister_subscriber=AsyncMock())
            host.api = SimpleNamespace(delivery=delivery)
            app = host._create_control_app()
            endpoint = next(route.endpoint for route in app.routes if route.path == "/runtime/events")
            response = await endpoint(SimpleNamespace(is_disconnected=AsyncMock(return_value=False)), user_id="alice")
            reading = asyncio.create_task(anext(response.body_iterator))
            try:
                await subscribed.wait()
                host._stop_event.set()
                with self.assertRaises(StopAsyncIteration):
                    await asyncio.wait_for(asyncio.shield(reading), timeout=0.5)
                delivery.unregister_subscriber.assert_awaited_once()
            finally:
                reading.cancel()
                await asyncio.gather(reading, return_exceptions=True)
                await response.body_iterator.aclose()

    async def test_web_disconnect_closes_silent_upstream_subscription(self):
        reading = asyncio.Event()
        disconnected = asyncio.Event()
        closed = asyncio.Event()
        class Stream:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                closed.set()
            def raise_for_status(self):
                pass
            async def aiter_lines(self):
                reading.set()
                await asyncio.Event().wait()
                yield ""
        class Client:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def stream(self, *args, **kwargs):
                return Stream()
        async def receive():
            await disconnected.wait()
            return {"type": "websocket.disconnect"}
        socket = SimpleNamespace(query_params={}, receive=receive, send_json=AsyncMock(),
            close=AsyncMock(), client_state=SimpleNamespace(name="CONNECTED"))
        with patch("xagent.core.runtime.ownership.runtime_is_active", return_value=True), patch(
            "xagent.core.runtime.client.RuntimeClient._client", return_value=Client()
        ):
            proxy = asyncio.create_task(_local_websocket(socket, "tasks", Path("/tmp/preview"), logging.getLogger(__name__)))
            try:
                await reading.wait()
                disconnected.set()
                await asyncio.wait_for(asyncio.shield(proxy), timeout=0.5)
                self.assertTrue(closed.is_set())
            finally:
                proxy.cancel()
                await asyncio.gather(proxy, return_exceptions=True)


class RestartDrainTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_background_owners_stop_concurrently(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = RuntimeHost(tmp)
            host.ownership.acquire()
            host._restarting = True
            release = asyncio.Event()
            started = [asyncio.Event(), asyncio.Event()]
            async def stop(index):
                started[index].set()
                await release.wait()
            host.heartbeat = SimpleNamespace(stop=lambda: stop(0))
            host.scheduler = SimpleNamespace(stop=lambda: stop(1))
            stopping = asyncio.create_task(host._shutdown())
            try:
                await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), timeout=0.5)
                release.set()
                await stopping
            finally:
                release.set()
                await asyncio.gather(stopping, return_exceptions=True)
                host._release_ownership_when_idle()

    async def test_closed_channel_cannot_keep_attention_spinning(self):
        attention = AttentionLoop(addressed_grace=0)
        activity = ChannelActivity()
        activity.quiesce()
        attention.attach_channel("weixin", activity)
        speak = AsyncMock()
        attention.register_speaker("weixin", speak)
        try:
            await attention.notice("weixin:room", addressed=True, content="pending")
            # No loop is started for blocked work; the event loop stays available.
            self.assertEqual(attention._next_due(), (None, 0.0))
            self.assertEqual(attention._armed_rooms(), [])
            await asyncio.sleep(0)
            speak.assert_not_called()
        finally:
            await attention.stop()

    async def test_restart_does_not_generate_new_diary_and_finalizes_receipts_once(self):
        for restart in (False, True):
            with self.subTest(restart=restart), tempfile.TemporaryDirectory() as tmp:
                host = RuntimeHost(tmp)
                host.ownership.acquire()
                host._restarting = restart
                memory = SimpleNamespace(run_maintenance=AsyncMock())
                turns = SimpleNamespace(recover=AsyncMock(return_value=0))
                host.agent = SimpleNamespace(inbox=SimpleNamespace(busy=False, pending_count=0,
                    stop_accepting=Mock(), cancel_pending=Mock()), abort=Mock(),
                    attention=SimpleNamespace(stop=AsyncMock()), memory_handler=memory, turn_store=turns)
                try:
                    await host._shutdown()
                    if restart:
                        memory.run_maintenance.assert_not_awaited()
                    else:
                        memory.run_maintenance.assert_awaited_once_with(force=True, trigger="shutdown")
                    turns.recover.assert_awaited_once()
                    self.assertIn("shutdown_seconds", host.status()["lifecycle_timings"])
                finally:
                    host._release_ownership_when_idle()

    async def test_stop_endpoint_marks_restart_without_changing_default_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = RuntimeHost(tmp)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host._create_control_app()), base_url="http://test") as client:
                self.assertEqual((await client.post("/runtime/stop", json={"restart": True})).status_code, 200)
                self.assertTrue(host._restarting)
                self.assertEqual((await client.post("/runtime/stop")).status_code, 200)
                self.assertFalse(host._restarting)

    async def test_readiness_does_not_read_memory_or_task_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = RuntimeHost(tmp)
            host.runtime_ready = True
            host.detailed_status = AsyncMock(side_effect=AssertionError("expensive status"))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host._create_control_app()), base_url="http://test") as client:
                result = await client.get("/runtime/ready")
            self.assertTrue(result.json()["runtime_ready"])
            host.detailed_status.assert_not_called()


class ProcessReadinessTests(unittest.TestCase):
    def test_ready_process_returns_without_fixed_survival_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            process = Mock(pid=123, returncode=None)
            process.poll.return_value = None
            probe = Mock(return_value=True)
            with patch("xagent.interfaces.cli.processes.running_pid", return_value=None), patch(
                "xagent.interfaces.cli.processes.subprocess.Popen", return_value=process
            ):
                result = start_background(["preview"], pid_path=root / "web.pid", log_path=root / "web.log", ready_probe=probe)
            self.assertTrue(result.ok)
            probe.assert_called_once_with(123)
            process.wait.assert_not_called()

    def test_readiness_timeout_retains_pid_to_prevent_duplicate_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            process = Mock(pid=123, returncode=None)
            process.poll.return_value = None
            with patch("xagent.interfaces.cli.processes.running_pid", return_value=None), patch(
                "xagent.interfaces.cli.processes.subprocess.Popen", return_value=process
            ):
                result = start_background(["preview"], pid_path=root / "web.pid", log_path=root / "web.log",
                    ready_probe=lambda pid: False, startup_timeout=0)
            self.assertFalse(result.ok)
            self.assertIn("readiness timed out", result.error)
            self.assertEqual((root / "web.pid").read_text().strip(), "123")

    def test_failed_child_cannot_be_reported_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            process = Mock(pid=123, returncode=1)
            process.poll.return_value = 1
            probe = Mock(return_value=True)
            with patch("xagent.interfaces.cli.processes.running_pid", return_value=None), patch(
                "xagent.interfaces.cli.processes.subprocess.Popen", return_value=process
            ):
                result = start_background(["preview"], pid_path=root / "web.pid", log_path=root / "web.log", ready_probe=probe)
            self.assertFalse(result.ok)
            probe.assert_not_called()
            self.assertFalse((root / "web.pid").exists())
