import asyncio
import tempfile
import unittest
from datetime import date
from pathlib import Path

from xagent.core.config import AgentConfig
from xagent.core.runtime import RuntimeHeartbeat, RuntimeHeartbeatConfig, create_runtime_heartbeat


from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock


class RuntimeHeartbeatConfigTests(unittest.TestCase):
    def test_defaults_are_enabled(self):
        config = RuntimeHeartbeatConfig.from_mapping(None)

        self.assertTrue(config.enabled)
        self.assertEqual(config.interval_seconds, AgentConfig.RUNTIME_HEARTBEAT_INTERVAL_SECONDS)

    def test_mapping_can_disable_heartbeat(self):
        config = RuntimeHeartbeatConfig.from_mapping({
            "heartbeat_enabled": False,
            "heartbeat_interval_seconds": 12,
        })

        self.assertFalse(config.enabled)
        self.assertEqual(config.interval_seconds, 12.0)

    def test_factory_returns_none_when_disabled(self):
        heartbeat = create_runtime_heartbeat(
            object(),
            {"heartbeat_enabled": False, "heartbeat_interval_seconds": 1},
        )

        self.assertIsNone(heartbeat)

    def test_factory_passes_subconscious_delivery_sink(self):
        class _Agent:
            subconscious_interval_seconds = 1800

            def __init__(self, workspace):
                self.workspace_dir = Path(workspace) / "workspace"

        async def sink(_delivery):
            return None

        with tempfile.TemporaryDirectory() as tmpdir:
            heartbeat = create_runtime_heartbeat(
                _Agent(tmpdir),
                {"heartbeat_enabled": True, "heartbeat_interval_seconds": 1},
                subconscious_delivery_sink=sink,
            )

        self.assertIsNotNone(heartbeat)
        self.assertIs(heartbeat._subconscious_loop._delivery_sink, sink)

    def test_factory_passes_subconscious_deliverable_channels(self):
        class _Agent:
            subconscious_interval_seconds = 1800

            def __init__(self, workspace):
                self.workspace_dir = Path(workspace) / "workspace"

        with tempfile.TemporaryDirectory() as tmpdir:
            heartbeat = create_runtime_heartbeat(
                _Agent(tmpdir),
                {"heartbeat_enabled": True, "heartbeat_interval_seconds": 1},
                subconscious_deliverable_channels={"api"},
            )

        self.assertIsNotNone(heartbeat)
        self.assertEqual(heartbeat._subconscious_loop._deliverable_channels, {"api"})

    def test_factory_resolves_contacts_to_agent_root_not_files_workspace(self):
        class _Agent:
            subconscious_interval_seconds = 1800

            def __init__(self, workspace):
                self.workspace = Path(workspace)
                self.workspace_dir = Path(workspace) / "workspace"

        with tempfile.TemporaryDirectory() as tmpdir:
            heartbeat = create_runtime_heartbeat(
                _Agent(tmpdir),
                {"heartbeat_enabled": True, "heartbeat_interval_seconds": 1},
            )

        self.assertIsNotNone(heartbeat)
        self.assertEqual(
            heartbeat._subconscious_loop.contacts_file,
            Path(tmpdir).resolve() / "contacts.json",
        )


class RuntimeHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def test_tick_only_wakes_workers_without_waiting_for_models(self):
        blocked = asyncio.Event()
        handler = SimpleNamespace(request_maintenance=Mock())
        reflection = SimpleNamespace(maybe_think=AsyncMock(side_effect=blocked.wait))
        today = date(2026, 1, 1)
        heartbeat = RuntimeHeartbeat(SimpleNamespace(memory_handler=handler),
            today_provider=lambda: today, subconscious_loop=reflection)
        await asyncio.wait_for(heartbeat.run_once(), 1)
        await asyncio.sleep(0)
        await heartbeat.run_once()
        self.assertEqual(handler.request_maintenance.call_count, 2)
        handler.request_maintenance.assert_called_with(today=today)
        reflection.maybe_think.assert_awaited_once()
        await heartbeat.stop()
        self.assertIsNone(heartbeat._reflection_task)

    async def test_slow_memory_does_not_delay_reflection(self):
        from xagent.core.agent import Agent
        with tempfile.TemporaryDirectory() as root:
            agent = Agent(client=object(), workspace=root)
            started, release = asyncio.Event(), asyncio.Event()
            async def maintain():
                started.set()
                await release.wait()
                return False
            agent.memory_handler.run_maintenance = maintain
            reflection = SimpleNamespace(maybe_think=AsyncMock())
            heartbeat = RuntimeHeartbeat(agent, subconscious_loop=reflection)
            await heartbeat.run_once()
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.sleep(0)
            reflection.maybe_think.assert_awaited_once()
            await heartbeat.stop()
            await agent.memory_handler.cancel_maintenance()

    async def test_memory_wakeup_error_does_not_suppress_reflection(self):
        handler = SimpleNamespace(request_maintenance=Mock(side_effect=RuntimeError("wakeup failed")))
        reflection = SimpleNamespace(maybe_think=AsyncMock())
        heartbeat = RuntimeHeartbeat(SimpleNamespace(memory_handler=handler), subconscious_loop=reflection)
        await heartbeat.run_once()
        await heartbeat._reflection_task
        reflection.maybe_think.assert_awaited_once()
        await heartbeat.stop()

    async def test_reflection_failure_still_flushes_observability(self):
        observability = SimpleNamespace(flush=AsyncMock())
        heartbeat = RuntimeHeartbeat(SimpleNamespace(observability=observability),
            subconscious_loop=SimpleNamespace(maybe_think=AsyncMock(side_effect=RuntimeError('model failed'))))
        await heartbeat.run_once()
        await heartbeat._reflection_task
        observability.flush.assert_awaited_once()
        await heartbeat.stop()

    async def test_interval_and_idempotent_stop(self):
        handler = SimpleNamespace(request_maintenance=Mock())
        heartbeat = RuntimeHeartbeat(SimpleNamespace(memory_handler=handler), interval_seconds=0.01)
        await heartbeat.start()
        try:
            for _ in range(50):
                if handler.request_maintenance.call_count >= 2:
                    break
                await asyncio.sleep(0.01)
            self.assertGreaterEqual(handler.request_maintenance.call_count, 2)
            self.assertTrue(heartbeat.is_running)
        finally:
            await heartbeat.stop()
            await heartbeat.stop()
        self.assertFalse(heartbeat.is_running)
