"""One host owns task execution and exposes unresolved occurrences."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from xagent.core.runtime.host import RuntimeHost
from xagent.core.runtime.ownership import runtime_owned_here
from xagent.core.runtime.tasks import AsyncTaskScheduler, enqueue_scheduled_task, list_task_records
from xagent.integrations.api.adapter import ApiChannelAdapter


class TaskRuntimeManagementTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_transport_lifecycle_never_constructs_a_scheduler(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            adapter = ApiChannelAdapter(SimpleNamespace(), contacts_file=root / "contacts.json",
                                        tasks_dir=root / "tasks")
            with patch("xagent.integrations.api.adapter.AsyncTaskScheduler") as scheduler:
                await adapter.start()
                await adapter.stop()
                await adapter.start()
                await adapter.stop()
            scheduler.assert_not_called()

    async def test_host_constructs_one_scheduler_after_ownership_for_all_channels(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            host = RuntimeHost(root, channels=["api", "feishu", "weixin", "voice"])
            agent = SimpleNamespace(
                memory_handler=SimpleNamespace(recover_pending_commit=AsyncMock()),
                turn_store=SimpleNamespace(recover=AsyncMock(return_value=0)),
                attention=SimpleNamespace(reset_to_present=AsyncMock()),
                message_storage=SimpleNamespace(), model="test-model", tools={},
            )
            runner = SimpleNamespace(agent=agent, tasks_dir=root / "tasks", config={})
            api = SimpleNamespace(agent=agent, tasks=SimpleNamespace())
            starts = []
            async def start_channel(channel):
                starts.append(channel)
                host.channel_status[channel] = {"state": "running"}
                if channel != "api":
                    host.adapters[channel] = SimpleNamespace(agent=host.agent)
                if len(starts) == 4:
                    host._stop_event.set()
            schedulers = []
            def construct_scheduler(*args, **kwargs):
                self.assertTrue(runtime_owned_here(root))
                self.assertTrue(all(adapter.agent is agent for adapter in host.adapters.values()))
                scheduler = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
                schedulers.append(scheduler)
                self.assertEqual(args, (runner.tasks_dir,))
                self.assertEqual(kwargs["can_handle"], host._can_handle_task)
                self.assertEqual(kwargs["execute"], host._execute_task)
                self.assertEqual(kwargs["deliver"], host._deliver_task)
                return scheduler
            async def shutdown():
                await host.scheduler.stop()
            with patch("xagent.interfaces.base.BaseAgentRunner", return_value=runner), patch(
                "xagent.integrations.api.ApiChannelAdapter", return_value=api,
            ), patch("xagent.interfaces.server.admin_service.AdminService"), patch(
                "xagent.core.runtime.AsyncTaskScheduler", side_effect=construct_scheduler,
            ) as factory, patch("xagent.core.runtime.create_runtime_heartbeat", return_value=None), patch.object(
                host, "_start_control_server", new_callable=AsyncMock,
            ), patch.object(host, "_start_channel", side_effect=start_channel), patch.object(
                host, "_shutdown", side_effect=shutdown,
            ):
                await host.run()
            factory.assert_called_once()
            schedulers[0].start.assert_awaited_once()
            schedulers[0].stop.assert_awaited_once()
            self.assertEqual(starts, ["api", "feishu", "weixin", "voice"])
            self.assertFalse(runtime_owned_here(root))

    async def test_detailed_runtime_status_and_task_view_preserve_review_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            now = datetime(2026, 6, 1, 10)
            task = enqueue_scheduled_task(tasks_dir=root / "tasks", task_type="agent", content="check",
                                          run_at=now, channel="api", target={"user_id": "alice"})
            async def execute(task):
                return {"content": "done"}
            async def deliver(task, result, run_id):
                raise TimeoutError("unknown external outcome")
            scheduler = AsyncTaskScheduler(root / "tasks", can_handle=lambda task: True,
                                            execute=execute, deliver=deliver, now_provider=lambda: now)
            await scheduler.tick()
            host = RuntimeHost(root)
            host.scheduler = scheduler
            status = await host.detailed_status()
            self.assertEqual(status["tasks"]["needs_review"], 1)
            self.assertEqual(status["tasks"]["failed"], 0)
            self.assertEqual(status["tasks"]["pending"], 0)
            view = list_task_records(root / "tasks")[0].to_task_view()
            self.assertEqual(view["task_id"], task.task_id)
            self.assertEqual(view["status"], "needs_review")
            self.assertEqual(view["run_stage"], "needs_review")
            self.assertTrue(view["run_id"])
            self.assertTrue(view["attempt_id"])
            self.assertIn("unknown external outcome", view["last_error"])
            self.assertIsNone(view["next_run_at"])
