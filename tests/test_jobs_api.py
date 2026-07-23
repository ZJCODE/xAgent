"""HTTP admin API tests for background jobs."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from xagent.components import MessageStorage
from xagent.core.handlers import MessageHandler
from xagent.interfaces.server import AgentHTTPServer


class _JobAgent:
    model = "test-model"
    tools = {}
    supports_vision = True

    def __init__(self, runtime_root: Path):
        self.workspace = runtime_root
        self.workspace_dir = runtime_root / "workspace"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.message_storage = MessageStorage(path=str(runtime_root / "messages" / "messages.db"))
        self.message_handler = MessageHandler(self.message_storage, workspace_dir=self.workspace_dir)

    async def observe(self, **kwargs):
        return None

    async def flush_memory(self):
        return None


class JobsApiTests(unittest.TestCase):
    def test_create_list_cancel_job(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            agent = _JobAgent(root)
            server = AgentHTTPServer(agent=agent, config_dir=str(root))
            with TestClient(server.app) as client:
                created = client.post(
                    "/api/jobs",
                    json={"command": "sleep 30", "title": "API job"},
                )
                self.assertEqual(created.status_code, 201, created.text)
                job_id = created.json()["job"]["job_id"]
                listed = client.get("/api/jobs?scope=current")
                self.assertEqual(listed.status_code, 200)
                self.assertGreaterEqual(listed.json()["total"], 1)
                cancelled = client.post(f"/api/jobs/{job_id}/cancel")
                self.assertEqual(cancelled.status_code, 200, cancelled.text)
                status = cancelled.json()["job"]["status"]
                self.assertIn(status, {"cancelled", "running", "queued"})
                terminal = status
                for _ in range(40):
                    detail = client.get(f"/api/jobs/{job_id}")
                    self.assertEqual(detail.status_code, 200, detail.text)
                    terminal = detail.json()["job"]["status"]
                    if terminal in {"cancelled", "failed", "completed"}:
                        break
                    import time
                    time.sleep(0.05)
                self.assertEqual(terminal, "cancelled")


if __name__ == "__main__":
    unittest.main()
