import tempfile
import unittest
import asyncio
from datetime import datetime

from xagent.core.runtime import (
    AsyncTaskScheduler,
    ScheduledDeliveryContext,
    enqueue_message_task,
    list_task_records,
    scheduled_delivery_context,
)
from xagent.tools.scheduler_tool import create_schedule_message_tool


class ScheduledMessageTaskTests(unittest.TestCase):
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
