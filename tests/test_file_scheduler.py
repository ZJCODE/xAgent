import tempfile
import unittest
import asyncio
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

    def test_schedule_task_tool_uses_current_delivery_context(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                tool = create_schedule_task_tool(tasks_dir=tmpdir)
                context = ScheduledDeliveryContext(
                    channel="feishu",
                    user_id="ou_user",
                    target={"chat_id": "oc_chat", "message_id": "om_anchor", "is_group": True},
                )
                with scheduled_delivery_context(context):
                    result = await tool(task_type="agent", content="查一下当前系统的温度然后发我", delay_seconds=60)
                records = list_task_records(tmpdir)

            self.assertTrue(result["scheduled"])
            self.assertEqual(result["channel"], "feishu")
            self.assertEqual(result["task_type"], "agent")
            self.assertEqual(records[0].target["chat_id"], "oc_chat")
            self.assertEqual(records[0].target["message_id"], "om_anchor")
            self.assertEqual(records[0].content, "查一下当前系统的温度然后发我")

        asyncio.run(run_test())

    def test_schedule_task_tool_validates_required_time_and_content(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                tool = create_schedule_task_tool(tasks_dir=tmpdir)
                missing_time = await tool(task_type="message", content="走两步")
                negative_delay = await tool(task_type="message", content="走两步", delay_seconds=-1)
                empty_content = await tool(task_type="message", content=" ", delay_seconds=60)

            self.assertFalse(missing_time["scheduled"])
            self.assertIn("run_at or delay_seconds", missing_time["error"])
            self.assertFalse(negative_delay["scheduled"])
            self.assertIn("zero or positive", negative_delay["error"])
            self.assertFalse(empty_content["scheduled"])
            self.assertIn("content", empty_content["error"])

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
