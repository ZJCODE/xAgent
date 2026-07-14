import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta

from xagent.core.runtime import (
    AsyncTaskScheduler,
    ScheduledDeliveryContext,
    enqueue_scheduled_task,
    list_archived_task_records,
    list_task_records,
    pause_scheduled_task,
    resolve_scheduled_task_run_at,
    resume_scheduled_task,
    scheduled_delivery_context,
    update_scheduled_task,
)
from xagent.tools.scheduler_tool import create_schedule_task_tool


class ScheduledTaskTests(unittest.TestCase):
    def test_enqueue_scheduled_task_writes_structured_json_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_at = datetime(2026, 6, 1, 14, 30, 0)
            task = enqueue_scheduled_task(
                task_type="message",
                content="走两步",
                run_at=run_at,
                tasks_dir=tmpdir,
                channel="api",
                target={"user_id": "web_user"},
                user_id="web_user",
                title="Reminder",
            )
            records = list_task_records(tmpdir)

        self.assertTrue(task.name.startswith("20260601-143000-"))
        self.assertEqual(records[0].kind, "task")
        self.assertEqual(records[0].task_type, "message")
        self.assertEqual(records[0].content, "走两步")
        self.assertEqual(records[0].target["channel"], "api")
        self.assertEqual(records[0].delivery["channel"], "api")

    def test_async_scheduler_dispatches_only_handleable_due_tasks(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                run_at = datetime(2026, 6, 1, 14, 30, 0)
                enqueue_scheduled_task(
                    task_type="message",
                    content="web reminder",
                    run_at=run_at,
                    tasks_dir=tmpdir,
                    channel="api",
                    target={"user_id": "web_user"},
                    user_id="web_user",
                )
                enqueue_scheduled_task(
                    task_type="message",
                    content="feishu reminder",
                    run_at=run_at,
                    tasks_dir=tmpdir,
                    channel="feishu",
                    target={"chat_id": "oc_x"},
                    user_id="ou_user",
                )
                delivered = []
                scheduler = AsyncTaskScheduler(
                    tmpdir,
                    can_handle=lambda task: task.delivery_channel == "api",
                    dispatch=lambda task: _append_delivered(delivered, task.content),
                    now_provider=lambda: run_at,
                )

                await scheduler.tick()
                records = list_task_records(tmpdir)

            self.assertEqual(delivered, ["web reminder"])
            self.assertEqual([record.content for record in records], ["feishu reminder"])

        async def _append_delivered(delivered, message):
            delivered.append(message)

        asyncio.run(run_test())

    def test_one_shot_success_is_archived_with_completion_metadata(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                run_at = datetime(2026, 6, 1, 14, 30, 0)
                original = enqueue_scheduled_task(
                    task_type="message",
                    content="archive me",
                    run_at=run_at,
                    tasks_dir=tmpdir,
                    channel="api",
                    target={"user_id": "web_user"},
                    user_id="web_user",
                )
                scheduler = AsyncTaskScheduler(
                    tmpdir,
                    can_handle=lambda task: True,
                    dispatch=lambda task: _noop(task),
                    now_provider=lambda: run_at,
                )
                await scheduler.tick()

                self.assertEqual(list_task_records(tmpdir, include_failed=False), [])
                archived = list_archived_task_records(tmpdir)
                self.assertEqual(len(archived), 1)
                self.assertEqual(archived[0].task_id, original.task_id)
                self.assertEqual(archived[0].status, "completed")
                self.assertEqual(archived[0].payload["completion_reason"], "one_shot_succeeded")
                self.assertEqual(archived[0].payload["last_run_status"], "succeeded")
                self.assertEqual(archived[0].path.parent.name, "2026-06")

        async def _noop(task):
            del task

        asyncio.run(run_test())

    def test_async_scheduler_quarantines_failed_agent_task(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                run_at = datetime(2026, 6, 1, 14, 30, 0)
                enqueue_scheduled_task(
                    task_type="agent",
                    content="Check system temperature",
                    run_at=run_at,
                    tasks_dir=tmpdir,
                    channel="api",
                    target={"user_id": "web_user"},
                    user_id="web_user",
                )
                scheduler = AsyncTaskScheduler(
                    tmpdir,
                    can_handle=lambda task: task.delivery_channel == "api",
                    dispatch=_raise_dispatch_error,
                    now_provider=lambda: run_at,
                )

                await scheduler.tick()
                records = list_task_records(tmpdir)

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].state, "failed")
            self.assertEqual(records[0].reason, "failed")
            self.assertEqual(records[0].task_type, "agent")

        async def _raise_dispatch_error(task):
            raise RuntimeError(f"boom: {task.content}")

        asyncio.run(run_test())

    def test_async_scheduler_reschedules_daily_task_to_next_future_time(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                original = enqueue_scheduled_task(
                    task_type="message",
                    content="写日报",
                    run_at=datetime(2026, 6, 1, 10, 0, 0),
                    tasks_dir=tmpdir,
                    channel="api",
                    target={"user_id": "web_user"},
                    user_id="web_user",
                    recurrence=[{"kind": "daily", "time": "10:00:00"}],
                    title="日报提醒",
                )
                delivered = []
                scheduler = AsyncTaskScheduler(
                    tmpdir,
                    can_handle=lambda task: task.delivery_channel == "api",
                    dispatch=lambda task: _append_delivered(delivered, task.content),
                    now_provider=lambda: datetime(2026, 6, 3, 15, 0, 0),
                )

                await scheduler.tick()
                records = list_task_records(tmpdir, include_failed=False)

            self.assertEqual(delivered, ["写日报"])
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].task_id, original.task_id)
            self.assertEqual(records[0].recurrence, [{"kind": "daily", "time": "10:00:00"}])
            self.assertEqual(records[0].run_at, datetime(2026, 6, 4, 10, 0, 0))

        async def _append_delivered(delivered, message):
            delivered.append(message)

        asyncio.run(run_test())

    def test_async_scheduler_reschedules_weekly_task_to_next_future_weekday(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                original = enqueue_scheduled_task(
                    task_type="message",
                    content="喝茶",
                    run_at=datetime(2026, 6, 3, 13, 28, 0),
                    tasks_dir=tmpdir,
                    channel="api",
                    target={"user_id": "web_user"},
                    user_id="web_user",
                    recurrence=[{"kind": "weekly", "time": "13:28:00", "weekdays": ["wed", "fri"]}],
                    title="喝茶提醒",
                )
                delivered = []
                scheduler = AsyncTaskScheduler(
                    tmpdir,
                    can_handle=lambda task: task.delivery_channel == "api",
                    dispatch=lambda task: _append_delivered(delivered, task.content),
                    now_provider=lambda: datetime(2026, 6, 5, 14, 0, 0),
                )

                await scheduler.tick()
                records = list_task_records(tmpdir, include_failed=False)

            self.assertEqual(delivered, ["喝茶"])
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].task_id, original.task_id)
            self.assertEqual(records[0].recurrence, [{"kind": "weekly", "time": "13:28:00", "weekdays": ["wed", "fri"]}])
            self.assertEqual(records[0].run_at, datetime(2026, 6, 10, 13, 28, 0))

        async def _append_delivered(delivered, message):
            delivered.append(message)

        asyncio.run(run_test())

    def test_async_scheduler_reschedules_interval_task_until_end_at(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                original = enqueue_scheduled_task(
                    task_type="message",
                    content="hey",
                    run_at=datetime(2026, 6, 1, 10, 10, 0),
                    tasks_dir=tmpdir,
                    channel="api",
                    target={"user_id": "web_user"},
                    user_id="web_user",
                    recurrence=[{"kind": "interval", "every_seconds": 600, "end_at": "2026-06-01 10:30:00"}],
                    title="Hey reminder",
                )
                delivered = []
                current = {"now": datetime(2026, 6, 1, 10, 10, 0)}
                scheduler = AsyncTaskScheduler(
                    tmpdir,
                    can_handle=lambda task: task.delivery_channel == "api",
                    dispatch=lambda task: _append_delivered(delivered, task.run_at),
                    now_provider=lambda: current["now"],
                )

                await scheduler.tick()
                first_records = list_task_records(tmpdir, include_failed=False)
                current["now"] = datetime(2026, 6, 1, 10, 20, 0)
                await scheduler.tick()
                second_records = list_task_records(tmpdir, include_failed=False)
                current["now"] = datetime(2026, 6, 1, 10, 30, 0)
                await scheduler.tick()
                final_records = list_task_records(tmpdir, include_failed=False)
                archived = list_archived_task_records(tmpdir)

            self.assertEqual(
                delivered,
                [
                    datetime(2026, 6, 1, 10, 10, 0),
                    datetime(2026, 6, 1, 10, 20, 0),
                    datetime(2026, 6, 1, 10, 30, 0),
                ],
            )
            self.assertEqual(first_records[0].task_id, original.task_id)
            self.assertEqual(first_records[0].run_at, datetime(2026, 6, 1, 10, 20, 0))
            self.assertEqual(second_records[0].run_at, datetime(2026, 6, 1, 10, 30, 0))
            self.assertEqual(final_records, [])
            self.assertEqual(archived[0].task_id, original.task_id)
            self.assertEqual(archived[0].payload["completion_reason"], "recurrence_exhausted")

        async def _append_delivered(delivered, run_at):
            delivered.append(run_at)

        asyncio.run(run_test())

    def test_pause_skips_dispatch_and_resume_advances_overdue_run_at(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                original = enqueue_scheduled_task(
                    task_type="message",
                    content="hey",
                    run_at=datetime(2026, 6, 1, 10, 0, 0),
                    tasks_dir=tmpdir,
                    channel="api",
                    target={"user_id": "web_user"},
                    user_id="web_user",
                    recurrence=[{"kind": "interval", "every_seconds": 60, "end_at": "2026-06-01 12:00:00"}],
                )
                paused = pause_scheduled_task(tmpdir, original.task_id)
                delivered = []
                scheduler = AsyncTaskScheduler(
                    tmpdir,
                    can_handle=lambda task: task.delivery_channel == "api",
                    dispatch=lambda task: _append_delivered(delivered, task.content),
                    now_provider=lambda: datetime(2026, 6, 1, 10, 0, 0),
                )
                await scheduler.tick()
                self.assertEqual(delivered, [])
                self.assertEqual(paused.status, "paused")

                resumed = resume_scheduled_task(
                    tmpdir,
                    original.task_id,
                    now=datetime(2026, 6, 1, 10, 5, 0),
                )
                self.assertEqual(resumed.status, "active")
                self.assertEqual(resumed.run_at, datetime(2026, 6, 1, 10, 6, 0))

                updated = update_scheduled_task(
                    tmpdir,
                    original.task_id,
                    content="记得喝水",
                    end_at="2026-06-01 13:00:00",
                )
                self.assertEqual(updated.content, "记得喝水")
                self.assertEqual(updated.recurrence[0]["end_at"], "2026-06-01 13:00:00")

        async def _append_delivered(delivered, message):
            delivered.append(message)

        asyncio.run(run_test())

    def test_async_scheduler_reschedules_failed_daily_task_on_dispatch_error(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                original = enqueue_scheduled_task(
                    task_type="message",
                    content="写日报",
                    run_at=datetime(2026, 6, 1, 10, 0, 0),
                    tasks_dir=tmpdir,
                    channel="api",
                    target={"user_id": "web_user"},
                    user_id="web_user",
                    recurrence=[{"kind": "daily", "time": "10:00:00"}],
                    title="日报提醒",
                )
                scheduler = AsyncTaskScheduler(
                    tmpdir,
                    can_handle=lambda task: task.delivery_channel == "api",
                    dispatch=_raise_dispatch_error,
                    now_provider=lambda: datetime(2026, 6, 1, 10, 0, 0),
                )

                await scheduler.tick()
                active_records = list_task_records(tmpdir, include_failed=False)
                all_records = list_task_records(tmpdir)

            self.assertEqual(len(active_records), 1)
            self.assertEqual(active_records[0].task_id, original.task_id)
            self.assertEqual(active_records[0].recurrence, [{"kind": "daily", "time": "10:00:00"}])
            self.assertEqual(active_records[0].run_at, datetime(2026, 6, 2, 10, 0, 0))
            self.assertEqual(len(all_records), 1)
            self.assertEqual(active_records[0].payload["last_run_status"], "failed")
            self.assertIn("boom", active_records[0].payload["last_error"])

        async def _raise_dispatch_error(task):
            raise RuntimeError(f"boom: {task.content}")

        asyncio.run(run_test())

    def test_final_recurring_failure_moves_to_attention_instead_of_archive(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                run_at = datetime(2026, 6, 1, 10, 30, 0)
                enqueue_scheduled_task(
                    task_type="message",
                    content="final attempt",
                    run_at=run_at,
                    tasks_dir=tmpdir,
                    channel="api",
                    target={"user_id": "web_user"},
                    recurrence=[{"kind": "interval", "every_seconds": 600, "end_at": "2026-06-01 10:30:00"}],
                )
                scheduler = AsyncTaskScheduler(
                    tmpdir,
                    can_handle=lambda task: True,
                    dispatch=_raise_dispatch_error,
                    now_provider=lambda: run_at,
                )
                await scheduler.tick()
                records = list_task_records(tmpdir)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0].status, "failed")
                self.assertEqual(records[0].payload["last_run_status"], "failed")
                self.assertEqual(list_archived_task_records(tmpdir), [])

        async def _raise_dispatch_error(task):
            raise RuntimeError(f"last run failed: {task.content}")

        asyncio.run(run_test())

    def test_archive_failure_is_quarantined_without_redispatch(self):
        from unittest.mock import patch

        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                run_at = datetime(2026, 6, 1, 10, 0, 0)
                enqueue_scheduled_task(
                    task_type="message",
                    content="deliver once",
                    run_at=run_at,
                    tasks_dir=tmpdir,
                    channel="api",
                    target={"user_id": "web_user"},
                )
                delivered = []

                async def deliver(task):
                    delivered.append(task.task_id)

                scheduler = AsyncTaskScheduler(
                    tmpdir,
                    can_handle=lambda task: True,
                    dispatch=deliver,
                    now_provider=lambda: run_at,
                )
                with patch("xagent.core.runtime.tasks._move_task_to_archive", side_effect=OSError("disk full")):
                    await scheduler.tick()
                await scheduler.tick()
                records = list_task_records(tmpdir)
                self.assertEqual(len(delivered), 1)
                self.assertEqual(records[0].status, "failed")
                self.assertEqual(records[0].reason, "completion_error")

        asyncio.run(run_test())

    def test_async_scheduler_reschedules_failed_weekly_task_on_dispatch_error(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                original = enqueue_scheduled_task(
                    task_type="message",
                    content="走路",
                    run_at=datetime(2026, 6, 3, 14, 28, 0),
                    tasks_dir=tmpdir,
                    channel="api",
                    target={"user_id": "web_user"},
                    user_id="web_user",
                    recurrence=[{"kind": "weekly", "time": "14:28:00", "weekdays": ["wed", "fri"]}],
                    title="走路提醒",
                )
                scheduler = AsyncTaskScheduler(
                    tmpdir,
                    can_handle=lambda task: task.delivery_channel == "api",
                    dispatch=_raise_dispatch_error,
                    now_provider=lambda: datetime(2026, 6, 3, 14, 28, 0),
                )

                await scheduler.tick()
                active_records = list_task_records(tmpdir, include_failed=False)
                all_records = list_task_records(tmpdir)

            self.assertEqual(len(active_records), 1)
            self.assertEqual(active_records[0].task_id, original.task_id)
            self.assertEqual(active_records[0].recurrence, [{"kind": "weekly", "time": "14:28:00", "weekdays": ["wed", "fri"]}])
            self.assertEqual(active_records[0].run_at, datetime(2026, 6, 5, 14, 28, 0))
            self.assertEqual(len(all_records), 1)

        async def _raise_dispatch_error(task):
            raise RuntimeError(f"boom: {task.content}")

        asyncio.run(run_test())

    def test_resolve_scheduled_task_run_at_materializes_interval_duration(self):
        now = datetime(2026, 6, 1, 11, 0, 0)

        run_at, recurrence = resolve_scheduled_task_run_at(
            recurrence=[{"kind": "interval", "every_seconds": 600, "duration_seconds": 18000}],
            now=now,
        )
        immediate_run_at, immediate_recurrence = resolve_scheduled_task_run_at(
            delay_seconds=0,
            recurrence=[{"kind": "interval", "every_seconds": 600, "duration_seconds": 18000}],
            now=now,
        )

        self.assertEqual(run_at, datetime(2026, 6, 1, 11, 10, 0))
        self.assertEqual(
            recurrence,
            [{"kind": "interval", "every_seconds": 600, "end_at": "2026-06-01 16:00:00"}],
        )
        self.assertEqual(immediate_run_at, now)
        self.assertEqual(immediate_recurrence[0]["end_at"], "2026-06-01 16:00:00")

    def test_manage_scheduled_tasks_create_uses_current_delivery_context(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                tool = create_schedule_task_tool(tasks_dir=tmpdir)
                context = ScheduledDeliveryContext(
                    channel="feishu",
                    user_id="ou_user",
                    target={"chat_id": "oc_chat", "message_id": "om_anchor", "is_group": True},
                )
                with scheduled_delivery_context(context):
                    result = await tool(action="create", task_type="agent", content="查一下当前系统的温度然后发我", delay_seconds=60)
                records = list_task_records(tmpdir)

            self.assertTrue(result["ok"])
            self.assertEqual(result["task"]["channel"], "feishu")
            self.assertEqual(result["task"]["task_type"], "agent")
            self.assertEqual(records[0].target["chat_id"], "oc_chat")
            self.assertEqual(records[0].target["message_id"], "om_anchor")
            self.assertEqual(records[0].content, "查一下当前系统的温度然后发我")

        asyncio.run(run_test())

    def test_manage_scheduled_tasks_creates_interval_task_with_convenience_params(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                tool = create_schedule_task_tool(tasks_dir=tmpdir)
                result = await tool(
                    action="create",
                    task_type="message",
                    content="hey",
                    interval_seconds=600,
                    duration_seconds=18000,
                    title="Hey reminder",
                )
                records = list_task_records(tmpdir)

            self.assertTrue(result["ok"])
            self.assertEqual(result["task"]["recurrence"][0]["kind"], "interval")
            self.assertEqual(result["task"]["recurrence"][0]["every_seconds"], 600)
            self.assertIn("end_at", result["task"]["recurrence"][0])
            self.assertNotIn("duration_seconds", result["task"]["recurrence"][0])
            self.assertEqual(records[0].recurrence, result["task"]["recurrence"])

        asyncio.run(run_test())

    def test_manage_scheduled_tasks_list_and_delete_by_task_id(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                tool = create_schedule_task_tool(tasks_dir=tmpdir)
                created = await tool(
                    action="create",
                    task_type="message",
                    content="记得走路",
                    recurrence=[{"kind": "weekly", "time": "10:00:00", "weekdays": ["wed", "fri"]}],
                    title="走路提醒",
                )
                listed = await tool(action="list")
                deleted = await tool(action="delete", task_id=created["task"]["task_id"])
                listed_again = await tool(action="list")
                records = list_task_records(tmpdir, include_failed=False)

            self.assertTrue(created["ok"])
            self.assertEqual(created["task"]["recurrence"], [{"kind": "weekly", "time": "10:00:00", "weekdays": ["wed", "fri"]}])
            self.assertEqual(created["task"]["status"], "active")
            self.assertEqual(listed["total"], 1)
            self.assertEqual(listed["tasks"][0]["task_id"], created["task"]["task_id"])
            self.assertEqual(listed["tasks"][0]["recurrence"], [{"kind": "weekly", "time": "10:00:00", "weekdays": ["wed", "fri"]}])
            self.assertEqual(records, [])
            self.assertTrue(deleted["ok"])
            self.assertEqual(deleted["deleted"]["task_id"], created["task"]["task_id"])
            self.assertEqual(listed_again["total"], 0)

        asyncio.run(run_test())

    def test_manage_scheduled_tasks_validates_required_time_and_content(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                tool = create_schedule_task_tool(tasks_dir=tmpdir)
                missing_time = await tool(action="create", task_type="message", content="走两步")
                negative_delay = await tool(action="create", task_type="message", content="走两步", delay_seconds=-1)
                empty_content = await tool(action="create", task_type="message", content=" ", delay_seconds=60)
                recurring_delay = await tool(
                    action="create",
                    task_type="message",
                    content="写日报",
                    delay_seconds=60,
                    recurrence=[{"kind": "daily", "time": "10:00:00"}],
                )
                daily_with_run_at = await tool(
                    action="create",
                    task_type="message",
                    content="写日报",
                    run_at="2026-06-01 10:00:00",
                    recurrence=[{"kind": "daily", "time": "10:00:00"}],
                )
                missing_weekly_time = await tool(
                    action="create",
                    task_type="message",
                    content="喝茶",
                    recurrence=[{"kind": "weekly", "weekdays": ["wed"]}],
                )
                weekly_with_delay = await tool(
                    action="create",
                    task_type="message",
                    content="喝茶",
                    delay_seconds=60,
                    recurrence=[{"kind": "weekly", "time": "13:28:00", "weekdays": ["wed"]}],
                )
                invalid_weekly_time = await tool(
                    action="create",
                    task_type="message",
                    content="喝茶",
                    recurrence=[{"kind": "weekly", "time": "2026-06-03 13:28:00", "weekdays": ["wed"]}],
                )
                invalid_weekly_missing_weekdays = await tool(
                    action="create",
                    task_type="message",
                    content="走路",
                    recurrence=[{"kind": "weekly", "time": "14:28:00"}],
                )
                invalid_recurrence_kind = await tool(
                    action="create",
                    task_type="message",
                    content="写日报",
                    recurrence=[{"kind": "monthly", "time": "10:00:00"}],
                )
                interval_too_short = await tool(
                    action="create",
                    task_type="message",
                    content="hey",
                    interval_seconds=30,
                    duration_seconds=300,
                )
                interval_missing_end = await tool(
                    action="create",
                    task_type="message",
                    content="hey",
                    interval_seconds=600,
                )
                interval_with_run_at = await tool(
                    action="create",
                    task_type="message",
                    content="hey",
                    run_at="2026-06-01 10:00:00",
                    interval_seconds=600,
                    duration_seconds=18000,
                )
                mixed_interval = await tool(
                    action="create",
                    task_type="message",
                    content="hey",
                    recurrence=[
                        {"kind": "daily", "time": "10:00:00"},
                        {"kind": "interval", "every_seconds": 600, "end_at": "2026-06-01 12:00:00"},
                    ],
                )
                missing_task_type = await tool(action="create", content="走两步", delay_seconds=60)
                missing_task_id = await tool(action="delete")

            self.assertFalse(missing_time["ok"])
            self.assertIn("run_at or delay_seconds", missing_time["error"])
            self.assertFalse(negative_delay["ok"])
            self.assertIn("zero or positive", negative_delay["error"])
            self.assertFalse(empty_content["ok"])
            self.assertIn("content", empty_content["error"])
            self.assertFalse(recurring_delay["ok"])
            self.assertIn("not supported", recurring_delay["error"])
            self.assertFalse(daily_with_run_at["ok"])
            self.assertIn("one-time tasks", daily_with_run_at["error"])
            self.assertFalse(missing_weekly_time["ok"])
            self.assertIn("run_at like HH:MM", missing_weekly_time["error"])
            self.assertFalse(weekly_with_delay["ok"])
            self.assertIn("not supported", weekly_with_delay["error"])
            self.assertFalse(invalid_weekly_time["ok"])
            self.assertIn("run_at like HH:MM", invalid_weekly_time["error"])
            self.assertFalse(invalid_weekly_missing_weekdays["ok"])
            self.assertIn("weekdays", invalid_weekly_missing_weekdays["error"])
            self.assertFalse(invalid_recurrence_kind["ok"])
            self.assertIn("kind must be one of", invalid_recurrence_kind["error"])
            self.assertFalse(interval_too_short["ok"])
            self.assertIn("at least", interval_too_short["error"])
            self.assertFalse(interval_missing_end["ok"])
            self.assertIn("ask the user", interval_missing_end["error"])
            self.assertFalse(interval_with_run_at["ok"])
            self.assertIn("one-time tasks", interval_with_run_at["error"])
            self.assertFalse(mixed_interval["ok"])
            self.assertIn("cannot be combined", mixed_interval["error"])
            self.assertFalse(missing_task_type["ok"])
            self.assertIn("task_type", missing_task_type["error"])
            self.assertFalse(missing_task_id["ok"])
            self.assertIn("task_id", missing_task_id["error"])

        asyncio.run(run_test())

    def test_resolve_interval_window_with_start_at(self):
        now = datetime(2026, 7, 10, 15, 0, 0)
        run_at, recurrence = resolve_scheduled_task_run_at(
            recurrence=[
                {
                    "kind": "interval",
                    "every_seconds": 600,
                    "start_at": "2026-07-11 10:00:00",
                    "end_at": "2026-07-11 12:00:00",
                }
            ],
            now=now,
        )
        self.assertEqual(run_at, datetime(2026, 7, 11, 10, 0, 0))
        self.assertEqual(
            recurrence,
            [
                {
                    "kind": "interval",
                    "every_seconds": 600,
                    "start_at": "2026-07-11 10:00:00",
                    "end_at": "2026-07-11 12:00:00",
                }
            ],
        )

    def test_async_scheduler_does_not_dispatch_before_interval_start_at(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                enqueue_scheduled_task(
                    task_type="message",
                    content="打球",
                    run_at=datetime(2026, 7, 11, 10, 0, 0),
                    tasks_dir=tmpdir,
                    channel="api",
                    target={"user_id": "web_user"},
                    user_id="web_user",
                    recurrence=[
                        {
                            "kind": "interval",
                            "every_seconds": 600,
                            "start_at": "2026-07-11 10:00:00",
                            "end_at": "2026-07-11 12:00:00",
                        }
                    ],
                )
                delivered = []
                scheduler = AsyncTaskScheduler(
                    tmpdir,
                    can_handle=lambda task: task.delivery_channel == "api",
                    dispatch=lambda task: _append_delivered(delivered, task.run_at),
                    now_provider=lambda: datetime(2026, 7, 10, 15, 0, 0),
                )

                await scheduler.tick()
                records = list_task_records(tmpdir, include_failed=False)

            self.assertEqual(delivered, [])
            self.assertEqual(records[0].run_at, datetime(2026, 7, 11, 10, 0, 0))

        async def _append_delivered(delivered, run_at):
            delivered.append(run_at)

        asyncio.run(run_test())

    def test_async_scheduler_interval_window_grid_sequence(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                enqueue_scheduled_task(
                    task_type="message",
                    content="打球",
                    run_at=datetime(2026, 7, 11, 10, 0, 0),
                    tasks_dir=tmpdir,
                    channel="api",
                    target={"user_id": "web_user"},
                    user_id="web_user",
                    recurrence=[
                        {
                            "kind": "interval",
                            "every_seconds": 600,
                            "start_at": "2026-07-11 10:00:00",
                            "end_at": "2026-07-11 12:00:00",
                        }
                    ],
                )
                delivered = []
                current = {"now": datetime(2026, 7, 11, 10, 0, 0)}
                scheduler = AsyncTaskScheduler(
                    tmpdir,
                    can_handle=lambda task: task.delivery_channel == "api",
                    dispatch=lambda task: _append_delivered(delivered, task.run_at),
                    now_provider=lambda: current["now"],
                )

                for minute in (0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120):
                    current["now"] = datetime(2026, 7, 11, 10, 0, 0) + timedelta(minutes=minute)
                    await scheduler.tick()

            self.assertEqual(
                delivered,
                [
                    datetime(2026, 7, 11, 10, 0, 0),
                    datetime(2026, 7, 11, 10, 10, 0),
                    datetime(2026, 7, 11, 10, 20, 0),
                    datetime(2026, 7, 11, 10, 30, 0),
                    datetime(2026, 7, 11, 10, 40, 0),
                    datetime(2026, 7, 11, 10, 50, 0),
                    datetime(2026, 7, 11, 11, 0, 0),
                    datetime(2026, 7, 11, 11, 10, 0),
                    datetime(2026, 7, 11, 11, 20, 0),
                    datetime(2026, 7, 11, 11, 30, 0),
                    datetime(2026, 7, 11, 11, 40, 0),
                    datetime(2026, 7, 11, 11, 50, 0),
                    datetime(2026, 7, 11, 12, 0, 0),
                ],
            )

        async def _append_delivered(delivered, run_at):
            delivered.append(run_at)

        asyncio.run(run_test())

    def test_pause_resume_aligns_interval_window_to_grid(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                original = enqueue_scheduled_task(
                    task_type="message",
                    content="打球",
                    run_at=datetime(2026, 7, 11, 10, 0, 0),
                    tasks_dir=tmpdir,
                    channel="api",
                    target={"user_id": "web_user"},
                    user_id="web_user",
                    recurrence=[
                        {
                            "kind": "interval",
                            "every_seconds": 600,
                            "start_at": "2026-07-11 10:00:00",
                            "end_at": "2026-07-11 12:00:00",
                        }
                    ],
                )
                pause_scheduled_task(tmpdir, original.task_id)
                resumed = resume_scheduled_task(
                    tmpdir,
                    original.task_id,
                    now=datetime(2026, 7, 11, 10, 23, 0),
                )
                self.assertEqual(resumed.run_at, datetime(2026, 7, 11, 10, 30, 0))

        asyncio.run(run_test())

    def test_interval_start_at_validation(self):
        with self.assertRaisesRegex(ValueError, "start_at must be before end_at"):
            resolve_scheduled_task_run_at(
                recurrence=[
                    {
                        "kind": "interval",
                        "every_seconds": 600,
                        "start_at": "2026-07-11 12:00:00",
                        "end_at": "2026-07-11 10:00:00",
                    }
                ],
                now=datetime(2026, 7, 10, 15, 0, 0),
            )

        with self.assertRaisesRegex(ValueError, "delay_seconds cannot be combined"):
            resolve_scheduled_task_run_at(
                delay_seconds=60,
                recurrence=[
                    {
                        "kind": "interval",
                        "every_seconds": 600,
                        "start_at": "2026-07-11 10:00:00",
                        "end_at": "2026-07-11 12:00:00",
                    }
                ],
                now=datetime(2026, 7, 10, 15, 0, 0),
            )


if __name__ == "__main__":
    unittest.main()
