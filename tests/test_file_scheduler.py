import asyncio
import tempfile
import unittest
from datetime import datetime

from xagent.core.runtime import (
    AsyncTaskScheduler,
    ScheduledDeliveryContext,
    enqueue_scheduled_task,
    list_task_records,
    scheduled_delivery_context,
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
                channel="web",
                target={"user_id": "web_user"},
                user_id="web_user",
                title="Reminder",
            )
            records = list_task_records(tmpdir)

        self.assertTrue(task.name.startswith("20260601-143000-"))
        self.assertEqual(records[0].kind, "task")
        self.assertEqual(records[0].task_type, "message")
        self.assertEqual(records[0].content, "走两步")
        self.assertEqual(records[0].target["channel"], "web")
        self.assertEqual(records[0].delivery["channel"], "web")

    def test_async_scheduler_dispatches_only_handleable_due_tasks(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                run_at = datetime(2026, 6, 1, 14, 30, 0)
                enqueue_scheduled_task(
                    task_type="message",
                    content="web reminder",
                    run_at=run_at,
                    tasks_dir=tmpdir,
                    channel="web",
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
                    can_handle=lambda task: task.delivery_channel == "web",
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

    def test_async_scheduler_quarantines_failed_agent_task(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                run_at = datetime(2026, 6, 1, 14, 30, 0)
                enqueue_scheduled_task(
                    task_type="agent",
                    content="Check system temperature",
                    run_at=run_at,
                    tasks_dir=tmpdir,
                    channel="web",
                    target={"user_id": "web_user"},
                    user_id="web_user",
                )
                scheduler = AsyncTaskScheduler(
                    tmpdir,
                    can_handle=lambda task: task.delivery_channel == "web",
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
                    channel="web",
                    target={"user_id": "web_user"},
                    user_id="web_user",
                    recurrence="daily",
                    title="日报提醒",
                )
                delivered = []
                scheduler = AsyncTaskScheduler(
                    tmpdir,
                    can_handle=lambda task: task.delivery_channel == "web",
                    dispatch=lambda task: _append_delivered(delivered, task.content),
                    now_provider=lambda: datetime(2026, 6, 3, 15, 0, 0),
                )

                await scheduler.tick()
                records = list_task_records(tmpdir, include_failed=False)

            self.assertEqual(delivered, ["写日报"])
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].task_id, original.task_id)
            self.assertEqual(records[0].recurrence, "daily")
            self.assertEqual(records[0].run_at, datetime(2026, 6, 4, 10, 0, 0))

        async def _append_delivered(delivered, message):
            delivered.append(message)

        asyncio.run(run_test())

    def test_async_scheduler_quarantines_failed_daily_task_without_reschedule(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                enqueue_scheduled_task(
                    task_type="message",
                    content="写日报",
                    run_at=datetime(2026, 6, 1, 10, 0, 0),
                    tasks_dir=tmpdir,
                    channel="web",
                    target={"user_id": "web_user"},
                    user_id="web_user",
                    recurrence="daily",
                    title="日报提醒",
                )
                scheduler = AsyncTaskScheduler(
                    tmpdir,
                    can_handle=lambda task: task.delivery_channel == "web",
                    dispatch=_raise_dispatch_error,
                    now_provider=lambda: datetime(2026, 6, 1, 10, 0, 0),
                )

                await scheduler.tick()
                active_records = list_task_records(tmpdir, include_failed=False)
                all_records = list_task_records(tmpdir)

            self.assertEqual(active_records, [])
            self.assertEqual(len(all_records), 1)
            self.assertEqual(all_records[0].state, "failed")
            self.assertEqual(all_records[0].recurrence, "daily")

        async def _raise_dispatch_error(task):
            raise RuntimeError(f"boom: {task.content}")

        asyncio.run(run_test())

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

    def test_manage_scheduled_tasks_list_and_delete_by_task_id(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                tool = create_schedule_task_tool(tasks_dir=tmpdir)
                created = await tool(
                    action="create",
                    task_type="message",
                    content="记得写日报",
                    run_at="10:00:00",
                    recurrence="daily",
                    title="日报提醒",
                )
                listed = await tool(action="list")
                deleted = await tool(action="delete", task_id=created["task"]["task_id"])
                listed_again = await tool(action="list")
                records = list_task_records(tmpdir, include_failed=False)

            self.assertTrue(created["ok"])
            self.assertEqual(created["task"]["recurrence"], "daily")
            self.assertEqual(created["task"]["status"], "active")
            self.assertEqual(listed["total"], 1)
            self.assertEqual(listed["tasks"][0]["task_id"], created["task"]["task_id"])
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
                    run_at="10:00:00",
                    delay_seconds=60,
                    recurrence="daily",
                )
                invalid_daily_time = await tool(
                    action="create",
                    task_type="message",
                    content="写日报",
                    run_at="2026-06-01 10:00:00",
                    recurrence="daily",
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
            self.assertFalse(invalid_daily_time["ok"])
            self.assertIn("HH:MM", invalid_daily_time["error"])
            self.assertFalse(missing_task_type["ok"])
            self.assertIn("task_type", missing_task_type["error"])
            self.assertFalse(missing_task_id["ok"])
            self.assertIn("task_id", missing_task_id["error"])

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
