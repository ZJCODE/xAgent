import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from xagent.components import MessageStorage
from xagent.core.handlers import MessageHandler
from xagent.core.runtime import ContactEntry, SubconsciousDelivery, enqueue_scheduled_task, list_task_records
from xagent.interfaces.server import AgentHTTPServer


class _TaskAgent:
    model = "test-model"
    tools = {}
    supports_vision = True

    def __init__(self, runtime_root: Path):
        self.workspace = runtime_root
        self.workspace_dir = runtime_root / "workspace"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.message_storage = MessageStorage(path=str(runtime_root / "messages" / "messages.db"))
        self.message_handler = MessageHandler(self.message_storage, workspace_dir=self.workspace_dir)
        self.chat_calls = []

    async def chat(self, **kwargs):
        self.chat_calls.append(kwargs)
        return "agent scheduled result"

    async def chat_events(self, **kwargs):
        self.chat_calls.append(kwargs)
        yield {
            "type": "message_done",
            "message_id": "scheduled-agent",
            "phase": "final",
            "content": "agent scheduled result",
        }
        yield {"type": "done"}

    async def flush_memory(self):
        return None


class _AttachmentTaskAgent(_TaskAgent):
    def __init__(self, runtime_root: Path):
        super().__init__(runtime_root)
        self.attachment = {
            "kind": "image",
            "path": "assets/generated/images/result.png",
            "blob_url": "/api/workspace/blob?path=assets%2Fgenerated%2Fimages%2Fresult.png",
            "mime_type": "image/png",
            "file_name": "result.png",
        }
        self.chat_event_calls = []

    async def chat_events(self, **kwargs):
        self.chat_event_calls.append(kwargs)
        yield {
            "type": "message_done",
            "message_id": "scheduled-image",
            "phase": "final",
            "content": "",
            "attachments": [self.attachment],
        }
        yield {"type": "done"}


class TaskApiTests(unittest.TestCase):
    def test_deliver_subconscious_message_broadcasts_to_subscriber(self):
        async def run_test():
            class _Subscriber:
                def __init__(self):
                    self.payloads = []

                async def send_json(self, payload):
                    self.payloads.append(payload)

            with tempfile.TemporaryDirectory() as tmpdir:
                agent = _TaskAgent(Path(tmpdir))
                server = AgentHTTPServer(agent=agent)
                subscriber = _Subscriber()
                await server._register_task_subscriber("web_user", subscriber)
                delivery = SubconsciousDelivery(
                    content="A direct subconscious note",
                    recipient=ContactEntry(
                        channel="api",
                        user_id="web_user",
                        target={"user_id": "web_user"},
                        last_seen="2026-06-25 09:00:00",
                    ),
                    internal_content="The inner thought",
                    created_at=datetime(2026, 6, 25, 9, 0, 0),
                )

                await server.deliver_subconscious_message(delivery)

            self.assertEqual(len(subscriber.payloads), 1)
            payload = subscriber.payloads[0]
            self.assertEqual(payload["type"], "subconscious_message")
            self.assertEqual(payload["content"], "A direct subconscious note")
            self.assertIn("message", payload)
            self.assertEqual(payload["message"]["role"], "assistant")

        import asyncio

        asyncio.run(run_test())

    def test_http_subconscious_delivery_rejects_feishu_channel(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                agent = _TaskAgent(Path(tmpdir))
                server = AgentHTTPServer(agent=agent)
                delivery = SubconsciousDelivery(
                    content="A direct subconscious note",
                    recipient=ContactEntry(
                        channel="feishu",
                        user_id="ou_123",
                        target={"chat_id": "oc_xxx", "sender_name": "张三"},
                        last_seen="2026-06-25 09:00:00",
                    ),
                    internal_content="The inner thought",
                    created_at=datetime(2026, 6, 25, 9, 0, 0),
                )

                with self.assertRaisesRegex(ValueError, "api runtime cannot deliver"):
                    await server.deliver_subconscious_message(delivery)

        import asyncio

        asyncio.run(run_test())

    def test_list_and_delete_task(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = _TaskAgent(Path(tmpdir))
            server = AgentHTTPServer(agent=agent)
            task = enqueue_scheduled_task(
                task_type="message",
                content="走两步",
                run_at="2099-01-01 00:00:00",
                tasks_dir=server.tasks_dir,
                channel="api",
                target={"user_id": "web_user"},
                user_id="web_user",
                title="Reminder",
            )
            with TestClient(server.app) as client:
                listed = client.get("/api/tasks")
                self.assertEqual(listed.status_code, 200)
                self.assertEqual(listed.json()["total"], 1)
                self.assertEqual(listed.json()["tasks"][0]["task_id"], task.task_id)
                self.assertEqual(listed.json()["tasks"][0]["channel"], "api")
                self.assertEqual(listed.json()["tasks"][0]["status"], "active")

                deleted = client.delete(f"/api/tasks/delete?task_id={task.task_id}")
                self.assertEqual(deleted.status_code, 200)
                self.assertEqual(deleted.json()["deleted"]["task_id"], task.task_id)

                listed_again = client.get("/api/tasks")
                self.assertEqual(listed_again.json()["total"], 0)

    def test_list_weekly_task_includes_weekdays(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = _TaskAgent(Path(tmpdir))
            server = AgentHTTPServer(agent=agent)
            task = enqueue_scheduled_task(
                task_type="message",
                content="喝茶",
                run_at="2099-01-07 13:28:00",
                tasks_dir=server.tasks_dir,
                channel="api",
                target={"user_id": "web_user"},
                user_id="web_user",
                title="喝茶提醒",
                recurrence=[{"kind": "weekly", "time": "13:28:00", "weekdays": ["wed", "fri"]}],
            )
            with TestClient(server.app) as client:
                listed = client.get("/api/tasks")
                self.assertEqual(listed.status_code, 200)
                self.assertEqual(listed.json()["total"], 1)
                self.assertEqual(listed.json()["tasks"][0]["task_id"], task.task_id)
                self.assertEqual(
                    listed.json()["tasks"][0]["recurrence"],
                    [{"kind": "weekly", "time": "13:28:00", "weekdays": ["wed", "fri"]}],
                )

    def test_create_task_endpoint_is_not_exposed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = _TaskAgent(Path(tmpdir))
            server = AgentHTTPServer(agent=agent)
            with TestClient(server.app) as client:
                response = client.post(
                    "/api/tasks/create",
                    json={"message": "走两步", "delay_seconds": 60, "user_id": "web_user"},
                )
                self.assertEqual(response.status_code, 404)

    def test_dispatch_scheduled_agent_task_broadcasts_agent_reply(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                agent = _TaskAgent(Path(tmpdir))
                server = AgentHTTPServer(agent=agent)
                enqueue_scheduled_task(
                    task_type="agent",
                    content="Check system temperature",
                    run_at="2026-06-01 14:30:00",
                    tasks_dir=server.tasks_dir,
                    channel="api",
                    target={"user_id": "web_user"},
                    user_id="web_user",
                    title="Temperature Check",
                )
                record = list_task_records(server.tasks_dir)[0]
                delivered = []

                async def capture_broadcast(task_record, content, *, stored_message=None, attachments=None):
                    delivered.append((task_record.task_type, content, stored_message, attachments))

                server.api.delivery.broadcast_scheduled_message = capture_broadcast
                await server._dispatch_scheduled_task(record)

            self.assertEqual(delivered, [("agent", "agent scheduled result", None, [])])
            self.assertEqual(agent.chat_calls[0]["user_id"], "web_user")
            self.assertIn("Check system temperature", agent.chat_calls[0]["user_message"])

        import asyncio

        asyncio.run(run_test())

    def test_dispatch_scheduled_agent_task_broadcasts_image_only_result(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                agent = _AttachmentTaskAgent(Path(tmpdir))
                server = AgentHTTPServer(agent=agent)
                enqueue_scheduled_task(
                    task_type="agent",
                    content="Generate a pointillism image",
                    run_at="2026-06-01 18:00:00",
                    tasks_dir=server.tasks_dir,
                    channel="api",
                    target={"user_id": "web_user"},
                    user_id="web_user",
                    title="Image Check",
                )
                record = list_task_records(server.tasks_dir)[0]
                delivered = []

                async def capture_broadcast(task_record, content, *, stored_message=None, attachments=None):
                    delivered.append((task_record.task_type, content, stored_message, attachments))

                server.api.delivery.broadcast_scheduled_message = capture_broadcast
                await server._dispatch_scheduled_task(record)

            self.assertEqual(delivered, [("agent", "", None, [agent.attachment])])
            self.assertEqual(agent.chat_event_calls[0]["user_id"], "web_user")
            self.assertFalse(agent.chat_event_calls[0]["stream"])

        import asyncio

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
