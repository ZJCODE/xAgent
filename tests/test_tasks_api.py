import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from xagent.components import MessageStorageLocal
from xagent.core.handlers import MessageHandler
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
    def test_create_list_and_delete_task(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = _TaskAgent(Path(tmpdir))
            server = AgentHTTPServer(agent=agent, enable_web=False)
            with TestClient(server.app) as client:
                created = client.post(
                    "/api/tasks/create",
                    json={"message": "走两步", "delay_seconds": 60, "user_id": "web_user"},
                )
                self.assertEqual(created.status_code, 200)
                task_name = created.json()["task"]["name"]

                listed = client.get("/api/tasks")
                self.assertEqual(listed.status_code, 200)
                self.assertEqual(listed.json()["total"], 1)
                self.assertEqual(listed.json()["tasks"][0]["payload"]["target"]["channel"], "web")

                deleted = client.delete(f"/api/tasks/delete?name={task_name}")
                self.assertEqual(deleted.status_code, 200)

                listed_again = client.get("/api/tasks")
                self.assertEqual(listed_again.json()["total"], 0)


if __name__ == "__main__":
    unittest.main()