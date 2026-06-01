import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from xagent.components import MessageStorageLocal
from xagent.core.handlers import MessageHandler
from xagent.core.runtime import enqueue_message_task
from xagent.interfaces.server import AgentHTTPServer


class _TaskAgent:
    model = "test-model"
    tools = {}
    supports_vision = True

    def __init__(self, runtime_root: Path):
        self.workspace = runtime_root
        self.workspace_dir = runtime_root / "workspace"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.message_storage = MessageStorageLocal(path=str(runtime_root / "messages" / "messages.db"))
        self.message_handler = MessageHandler(self.message_storage, workspace_dir=self.workspace_dir)

    async def flush_memory(self):
        return None


class TaskApiTests(unittest.TestCase):
    def test_list_and_delete_task(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = _TaskAgent(Path(tmpdir))
            server = AgentHTTPServer(agent=agent, enable_web=False)
            task = enqueue_message_task(
                message="走两步",
                run_at="2099-01-01 00:00:00",
                tasks_dir=server.tasks_dir,
                target={"channel": "web", "user_id": "web_user"},
                user_id="web_user",
                title="Reminder",
            )
            with TestClient(server.app) as client:
                listed = client.get("/api/tasks")
                self.assertEqual(listed.status_code, 200)
                self.assertEqual(listed.json()["total"], 1)
                self.assertEqual(listed.json()["tasks"][0]["payload"]["target"]["channel"], "web")

                deleted = client.delete(f"/api/tasks/delete?name={task.name}")
                self.assertEqual(deleted.status_code, 200)

                listed_again = client.get("/api/tasks")
                self.assertEqual(listed_again.json()["total"], 0)

    def test_create_task_endpoint_is_not_exposed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = _TaskAgent(Path(tmpdir))
            server = AgentHTTPServer(agent=agent, enable_web=False)
            with TestClient(server.app) as client:
                response = client.post(
                    "/api/tasks/create",
                    json={"message": "走两步", "delay_seconds": 60, "user_id": "web_user"},
                )
                self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
