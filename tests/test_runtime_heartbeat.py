import asyncio
import unittest
from datetime import date

from xagent.core.config import AgentConfig
from xagent.core.runtime import RuntimeHeartbeat, RuntimeHeartbeatConfig, create_runtime_heartbeat


class _FakeMemoryHandler:
    def __init__(self):
        self.weekly_calls = []
        self.weekly_event = asyncio.Event()
        self.raise_on_weekly = False

    async def generate_previous_weekly_summary_if_missing(self, today=None):
        if self.raise_on_weekly:
            raise RuntimeError("weekly failed")
        self.weekly_calls.append(today)
        self.weekly_event.set()
        return True


class _FakeAgent:
    def __init__(self):
        self.flush_count = 0
        self.flush_event = asyncio.Event()
        self.raise_on_flush = False
        self.memory_handler = _FakeMemoryHandler()

    async def flush_memory(self):
        if self.raise_on_flush:
            raise RuntimeError("flush failed")
        self.flush_count += 1
        self.flush_event.set()


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


class RuntimeHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_once_flushes_without_weekly_on_non_monday(self):
        agent = _FakeAgent()
        heartbeat = RuntimeHeartbeat(agent, today_provider=lambda: date(2026, 5, 14))

        await heartbeat.run_once()

        self.assertEqual(agent.flush_count, 1)
        self.assertEqual(agent.memory_handler.weekly_calls, [])

    async def test_run_once_generates_previous_weekly_summary_on_monday(self):
        agent = _FakeAgent()
        today = date(2026, 5, 18)
        heartbeat = RuntimeHeartbeat(agent, today_provider=lambda: today)

        await heartbeat.run_once()

        self.assertEqual(agent.flush_count, 1)
        self.assertEqual(agent.memory_handler.weekly_calls, [today])

    async def test_run_once_isolates_maintenance_errors(self):
        agent = _FakeAgent()
        agent.raise_on_flush = True
        agent.memory_handler.raise_on_weekly = True
        heartbeat = RuntimeHeartbeat(agent, today_provider=lambda: date(2026, 5, 18))

        await heartbeat.run_once()

        self.assertEqual(agent.flush_count, 0)
        self.assertEqual(agent.memory_handler.weekly_calls, [])

    async def test_start_runs_immediate_tick_and_stop_is_idempotent(self):
        agent = _FakeAgent()
        heartbeat = RuntimeHeartbeat(
            agent,
            interval_seconds=60,
            today_provider=lambda: date(2026, 5, 14),
        )

        await heartbeat.start()
        await asyncio.wait_for(agent.flush_event.wait(), timeout=1)
        self.assertTrue(heartbeat.is_running)

        await heartbeat.stop()
        await heartbeat.stop()
        self.assertFalse(heartbeat.is_running)

    async def test_loop_runs_on_interval(self):
        agent = _FakeAgent()
        heartbeat = RuntimeHeartbeat(
            agent,
            interval_seconds=0.01,
            today_provider=lambda: date(2026, 5, 14),
        )

        await heartbeat.start()
        try:
            for _ in range(50):
                if agent.flush_count >= 2:
                    break
                await asyncio.sleep(0.01)
            self.assertGreaterEqual(agent.flush_count, 2)
        finally:
            await heartbeat.stop()


if __name__ == "__main__":
    unittest.main()
