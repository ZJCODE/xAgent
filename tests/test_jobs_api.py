"""HTTP admin API tests for transactional background jobs."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from xagent.components import MessageStorage
from xagent.core.handlers import MessageHandler
from xagent.core.runtime import JobStore
from xagent.interfaces.server import AgentHTTPServer
from xagent.interfaces.server.admin_routes import register_admin_routes


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


class _AdminOnly:
    def __init__(self, root: Path):
        self.jobs_dir = root / "jobs"
        self.workspace_dir = root / "workspace"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.config = {
            "jobs": {
                "worker_idle_seconds": 2,
                "runner_heartbeat_seconds": 0.2,
                "runner_stale_seconds": 2,
                "cancel_grace_seconds": 0.2,
            }
        }


def _wait_for_terminal(client: TestClient, job_id: str, timeout: float = 6.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()["job"]
        if job["status"] not in {"queued", "starting", "running", "cancelling"}:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not reach a terminal state")


class JobsApiTests(unittest.TestCase):
    def test_create_list_cancel_job(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            agent = _JobAgent(root)
            server = AgentHTTPServer(agent=agent, config_dir=str(root))
            with TestClient(server.app) as client:
                created = client.post(
                    "/api/jobs",
                    json={
                        "argv": ["python", "-c", "import time; time.sleep(30)"],
                        "title": "API job",
                    },
                )
                self.assertEqual(created.status_code, 201, created.text)
                self.assertTrue(created.json()["worker"]["available"])
                job_id = created.json()["job"]["job_id"]
                listed = client.get("/api/jobs?scope=active")
                self.assertEqual(listed.status_code, 200)
                self.assertGreaterEqual(listed.json()["total"], 1)
                cancelled = client.post(f"/api/jobs/{job_id}/cancel")
                self.assertEqual(cancelled.status_code, 200, cancelled.text)
                self.assertIn(cancelled.json()["job"]["status"], {"cancelled", "cancelling"})
                terminal = _wait_for_terminal(client, job_id)
                self.assertEqual(terminal["status"], "cancelled")

    def test_create_without_channel_runtime_still_executes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            admin = _AdminOnly(Path(tmpdir))
            app = FastAPI()
            register_admin_routes(app, lambda: admin)
            with TestClient(app) as client:
                created = client.post(
                    "/api/jobs",
                    json={"argv": ["python", "-c", "print('orphan-safe')"], "title": "Independent"},
                )
                self.assertEqual(created.status_code, 201, created.text)
                job = _wait_for_terminal(client, created.json()["job"]["job_id"])
                self.assertEqual(job["status"], "succeeded")
                self.assertIn("orphan-safe", job["stdout_tail"])

    def test_idempotency_key_reuses_only_identical_spec(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            admin = _AdminOnly(Path(tmpdir))
            app = FastAPI()
            register_admin_routes(app, lambda: admin)
            with TestClient(app) as client:
                headers = {"Idempotency-Key": "stable-request"}
                first = client.post("/api/jobs", headers=headers, json={"argv": ["true"]})
                second = client.post("/api/jobs", headers=headers, json={"argv": ["true"]})
                conflict = client.post("/api/jobs", headers=headers, json={"argv": ["false"]})
                metadata_conflict = client.post(
                    "/api/jobs",
                    headers=headers,
                    json={"argv": ["true"], "title": "different request"},
                )
                self.assertEqual(first.status_code, 201, first.text)
                self.assertEqual(second.status_code, 201, second.text)
                self.assertEqual(
                    first.json()["job"]["job_id"],
                    second.json()["job"]["job_id"],
                )
                self.assertEqual(conflict.status_code, 409, conflict.text)
                self.assertEqual(metadata_conflict.status_code, 409, metadata_conflict.text)

    def test_shell_requires_explicit_opt_in_and_same_origin(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            admin = _AdminOnly(Path(tmpdir))
            app = FastAPI()
            register_admin_routes(app, lambda: admin)
            with TestClient(app) as client:
                implicit = client.post("/api/jobs", json={"command": "echo unsafe"})
                self.assertEqual(implicit.status_code, 422)
                cross_origin = client.post(
                    "/api/jobs",
                    headers={"Origin": "https://evil.example"},
                    json={"command": "echo explicit", "shell": True},
                )
                self.assertEqual(cross_origin.status_code, 403)
            with TestClient(app, client=("203.0.113.10", 50000)) as remote_client:
                remote = remote_client.post(
                    "/api/jobs",
                    json={"argv": ["true"]},
                )
                self.assertEqual(remote.status_code, 403)
                store = JobStore(
                    admin.jobs_dir,
                    workspace_dir=admin.workspace_dir,
                )
                terminal = store.request_cancel(
                    store.create_job(argv=["true"], channel="api", target={}).job_id
                )
                self.assertEqual(
                    remote_client.post(f"/api/jobs/{terminal.job_id}/cancel").status_code,
                    403,
                )
                self.assertEqual(
                    remote_client.delete(f"/api/jobs/{terminal.job_id}").status_code,
                    403,
                )

    def test_failed_job_can_retry_and_terminal_job_can_delete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            admin = _AdminOnly(Path(tmpdir))
            app = FastAPI()
            register_admin_routes(app, lambda: admin)
            with TestClient(app) as client:
                created = client.post("/api/jobs", json={"argv": ["false"]})
                failed = _wait_for_terminal(client, created.json()["job"]["job_id"])
                self.assertEqual(failed["status"], "failed")
                retried = client.post(f"/api/jobs/{failed['job_id']}/retry")
                self.assertEqual(retried.status_code, 201, retried.text)
                self.assertEqual(retried.json()["job"]["retry_of"], failed["job_id"])
                deleted = client.delete(f"/api/jobs/{failed['job_id']}")
                self.assertEqual(deleted.status_code, 200, deleted.text)


if __name__ == "__main__":
    unittest.main()
