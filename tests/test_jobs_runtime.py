"""Tests for background job runtime and tool."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from xagent.core.runtime import (
    AsyncJobSupervisor,
    ScheduledDeliveryContext,
    enqueue_job,
    get_job,
    list_archived_job_records,
    list_job_records,
    request_job_cancel,
    scheduled_delivery_context,
)
from xagent.tools.jobs_tool import create_manage_jobs_tool


class BackgroundJobTests(unittest.TestCase):
    def test_enqueue_job_writes_control_plane_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            job = enqueue_job(
                kind="process",
                command="echo hello",
                jobs_dir=tmpdir,
                channel="api",
                target={"user_id": "web_user"},
                user_id="web_user",
                title="Hello",
            )
            records = list_job_records(tmpdir)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].job_id, job.job_id)
            self.assertEqual(records[0].status, "queued")
            self.assertEqual(records[0].command, "echo hello")
            self.assertTrue((Path(tmpdir) / job.job_id / "work").is_dir())
            self.assertFalse((Path(tmpdir) / "workspace").exists())

    def test_supervisor_runs_process_and_archives(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                jobs_dir = Path(tmpdir) / "jobs"
                workspace = Path(tmpdir) / "workspace"
                workspace.mkdir()
                job = enqueue_job(
                    kind="process",
                    command="python3 -c \"print('done')\"",
                    jobs_dir=jobs_dir,
                    channel="api",
                    target={"user_id": "web_user"},
                    user_id="web_user",
                    title="Print",
                )
                notified = []

                async def notify(record):
                    notified.append(record.job_id)

                supervisor = AsyncJobSupervisor(
                    jobs_dir,
                    can_notify=lambda record: record.delivery_channel == "api",
                    notify=notify,
                    workspace_dir=workspace,
                    poll_interval_seconds=0.05,
                )
                await supervisor.start()
                for _ in range(80):
                    active = list_job_records(jobs_dir, include_failed=False, include_claimed=True)
                    if not active:
                        break
                    await asyncio.sleep(0.05)
                await supervisor.stop()

                archived = list_archived_job_records(jobs_dir)
                self.assertEqual(len(archived), 1)
                self.assertEqual(archived[0].job_id, job.job_id)
                self.assertEqual(archived[0].status, "completed")
                self.assertEqual(notified, [job.job_id])
                stdout = (jobs_dir / job.job_id / "stdout.log").read_text(encoding="utf-8")
                self.assertIn("done", stdout)

        asyncio.run(run_test())

    def test_cancel_queued_job_archives_immediately(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            job = enqueue_job(
                kind="process",
                command="sleep 30",
                jobs_dir=tmpdir,
                channel="api",
                target={"user_id": "web_user"},
                user_id="web_user",
            )
            cancelled = request_job_cancel(tmpdir, job.job_id)
            self.assertEqual(cancelled.status, "cancelled")
            self.assertEqual(list_job_records(tmpdir, include_failed=False), [])
            archived = list_archived_job_records(tmpdir)
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0].status, "cancelled")

    def test_cancel_running_job(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                jobs_dir = Path(tmpdir) / "jobs"
                workspace = Path(tmpdir) / "workspace"
                workspace.mkdir()
                job = enqueue_job(
                    kind="process",
                    command="python3 -c \"import time; time.sleep(30)\"",
                    jobs_dir=jobs_dir,
                    channel="api",
                    target={"user_id": "web_user"},
                    user_id="web_user",
                )
                supervisor = AsyncJobSupervisor(
                    jobs_dir,
                    can_notify=lambda record: False,
                    notify=lambda record: asyncio.sleep(0),
                    workspace_dir=workspace,
                    poll_interval_seconds=0.05,
                    cancel_grace_seconds=0.2,
                )
                await supervisor.start()
                running = None
                for _ in range(80):
                    records = list_job_records(jobs_dir, include_claimed=True)
                    if records and records[0].status == "running":
                        running = records[0]
                        break
                    await asyncio.sleep(0.05)
                self.assertIsNotNone(running)
                request_job_cancel(jobs_dir, job.job_id)
                supervisor.wake()
                for _ in range(80):
                    if not list_job_records(jobs_dir, include_failed=False, include_claimed=True):
                        break
                    await asyncio.sleep(0.05)
                await supervisor.stop()
                archived = list_archived_job_records(jobs_dir)
                self.assertEqual(len(archived), 1)
                self.assertEqual(archived[0].status, "cancelled")

        asyncio.run(run_test())

    def test_manage_jobs_tool_start_uses_delivery_context(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                woke = []
                tool = create_manage_jobs_tool(jobs_dir=tmpdir, wake=lambda: woke.append(True))
                with scheduled_delivery_context(
                    ScheduledDeliveryContext(
                        channel="api",
                        user_id="web_user",
                        target={"user_id": "web_user"},
                        metadata={"source": "test"},
                    )
                ):
                    result = await tool(action="start", command="echo hi", title="Tool job")
                self.assertTrue(result["ok"])
                self.assertEqual(result["job"]["channel"], "api")
                self.assertEqual(result["job"]["user_id"], "web_user")
                self.assertEqual(woke, [True])
                listed = await tool(action="list", scope="current")
                self.assertEqual(listed["total"], 1)

        asyncio.run(run_test())

    def test_job_does_not_require_chat_slot_semantics(self):
        """Supervisor notify path is independent of ChatService.acquire_slot."""
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                jobs_dir = Path(tmpdir) / "jobs"
                workspace = Path(tmpdir) / "workspace"
                workspace.mkdir()
                enqueue_job(
                    kind="process",
                    command="true",
                    jobs_dir=jobs_dir,
                    channel="api",
                    target={"user_id": "web_user"},
                    user_id="web_user",
                )
                slot_calls = {"acquire": 0}

                async def notify(record):
                    # Intentionally no chat slot acquire — jobs must stay off the chat budget.
                    self.assertEqual(slot_calls["acquire"], 0)
                    get_job(jobs_dir, record.job_id)

                supervisor = AsyncJobSupervisor(
                    jobs_dir,
                    can_notify=lambda record: True,
                    notify=notify,
                    workspace_dir=workspace,
                    poll_interval_seconds=0.05,
                )
                await supervisor.start()
                for _ in range(40):
                    if list_archived_job_records(jobs_dir):
                        break
                    await asyncio.sleep(0.05)
                await supervisor.stop()
                self.assertEqual(len(list_archived_job_records(jobs_dir)), 1)

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
