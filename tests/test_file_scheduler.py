import tempfile
import unittest
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from xagent.core.runtime import (
    AsyncTaskScheduler,
    ScheduledDeliveryContext,
    enqueue_message_task,
    list_task_records,
    scheduled_delivery_context,
)
from xagent.core.runtime.scheduler import FileScheduler, enqueue_command, list_scheduled_tasks, parse_run_at
from xagent.tools.scheduler_tool import create_schedule_message_tool


class FileSchedulerTests(unittest.TestCase):
    def test_enqueue_uses_calendar_timestamp_and_unique_collision_suffix(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_at = parse_run_at("2026-06-01 14:30:00")

            first = enqueue_command("echo first", run_at, tmpdir)
            second = enqueue_command("echo second", run_at, tmpdir)
            tasks = list_scheduled_tasks(tmpdir, include_commands=True)

        self.assertEqual(first.name, "20260601-143000.sh")
        self.assertTrue(second.name.startswith("20260601-143000-"))
        self.assertCountEqual([task.command for task in tasks], ["echo first", "echo second"])

    def test_tick_executes_due_task_in_workspace_and_deletes_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            tasks_dir = root / "tasks"
            workspace_dir = root / "workspace"
            run_at = datetime(2026, 6, 1, 14, 30, 0)
            enqueue_command("pwd > done.txt", run_at, tasks_dir)
            scheduler = FileScheduler(
                tasks_dir,
                working_directory=workspace_dir,
                now_provider=lambda: run_at + timedelta(seconds=1),
            )

            tick = scheduler.tick(wait=True)

            self.assertEqual(tick.dispatched, 1)
            self.assertEqual(
                Path((workspace_dir / "done.txt").read_text(encoding="utf-8").strip()).resolve(),
                workspace_dir.resolve(),
            )
            self.assertEqual(list(tasks_dir.glob("*.sh")), [])

    def test_failed_task_is_quarantined(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tasks_dir = Path(tmpdir) / "tasks"
            run_at = datetime(2026, 6, 1, 14, 30, 0)
            enqueue_command("exit 7", run_at, tasks_dir)
            scheduler = FileScheduler(tasks_dir, now_provider=lambda: run_at)

            tick = scheduler.tick(wait=True)
            failed_files = sorted((tasks_dir / "failed").iterdir())

        self.assertEqual(tick.dispatched, 1)
        self.assertEqual([path.name for path in failed_files], ["20260601-143000.sh.failed"])

    def test_recover_running_tasks_requeues_interrupted_claims(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tasks_dir = Path(tmpdir) / "tasks"
            task = enqueue_command("echo again", datetime(2026, 6, 1, 14, 30, 0), tasks_dir)
            running_path = task.path.with_name(f"{task.name}.running-test")
            task.path.rename(running_path)
            scheduler = FileScheduler(tasks_dir)

            recovered = scheduler.recover_running_tasks()

            self.assertEqual(recovered, 1)
            self.assertTrue((tasks_dir / task.name).is_file())
            self.assertFalse(running_path.exists())

    def test_sleep_duration_is_bounded_for_far_future_tasks(self):
        now = datetime(2026, 6, 1, 12, 0, 0)
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = FileScheduler(
                tmpdir,
                now_provider=lambda: now,
                poll_interval_seconds=2.5,
            )

            duration = scheduler.sleep_duration(now + timedelta(hours=1))

        self.assertEqual(duration, 2.5)

    def test_enqueue_message_task_writes_structured_json_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_at = datetime(2026, 6, 1, 14, 30, 0)
            task = enqueue_message_task(
                message="走两步",
                run_at=run_at,
                tasks_dir=tmpdir,
                target={"channel": "web", "user_id": "web_user"},
                user_id="web_user",
                title="Reminder",
            )
            records = list_task_records(tmpdir)

        self.assertTrue(task.name.startswith("20260601-143000-"))
        self.assertEqual(records[0].kind, "message")
        self.assertEqual(records[0].message, "走两步")
        self.assertEqual(records[0].target["channel"], "web")

    def test_async_scheduler_dispatches_only_handleable_due_messages(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                run_at = datetime(2026, 6, 1, 14, 30, 0)
                enqueue_message_task(
                    message="web reminder",
                    run_at=run_at,
                    tasks_dir=tmpdir,
                    target={"channel": "web", "user_id": "web_user"},
                    user_id="web_user",
                )
                enqueue_message_task(
                    message="feishu reminder",
                    run_at=run_at,
                    tasks_dir=tmpdir,
                    target={"channel": "feishu", "chat_id": "oc_x"},
                    user_id="ou_user",
                )
                delivered = []
                scheduler = AsyncTaskScheduler(
                    tmpdir,
                    can_handle=lambda task: task.target.get("channel") == "web",
                    dispatch=lambda task: _append_delivered(delivered, task.message),
                    now_provider=lambda: run_at,
                )

                await scheduler.tick()
                records = list_task_records(tmpdir)

            self.assertEqual(delivered, ["web reminder"])
            self.assertEqual([record.message for record in records], ["feishu reminder"])

        async def _append_delivered(delivered, message):
            delivered.append(message)

        asyncio.run(run_test())

    def test_schedule_message_tool_uses_current_delivery_context(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                tool = create_schedule_message_tool(tasks_dir=tmpdir)
                context = ScheduledDeliveryContext(
                    channel="feishu",
                    user_id="ou_user",
                    target={"chat_id": "oc_chat", "message_id": "om_anchor", "is_group": True},
                )
                with scheduled_delivery_context(context):
                    result = await tool(message="走两步", delay_seconds=60)
                records = list_task_records(tmpdir)

            self.assertTrue(result["scheduled"])
            self.assertEqual(result["channel"], "feishu")
            self.assertEqual(records[0].target["chat_id"], "oc_chat")
            self.assertEqual(records[0].target["message_id"], "om_anchor")

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
