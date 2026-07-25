"""Tests for background job runtime and tool."""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path

from xagent.core.runtime import (
    AsyncJobSupervisor,
    ScheduledDeliveryContext,
    delete_job,
    enqueue_job,
    get_job,
    has_live_job_supervisor,
    list_archived_job_records,
    list_job_records,
    request_job_cancel,
    scheduled_delivery_context,
)
from xagent.core.runtime.jobs import (
    CLAIM_MARKER,
    JOB_STATUS_CLAIMED,
    JOB_STATUS_RUNNING,
    _release_resource_locks,
    _try_acquire_resource_locks,
    _write_json_atomic,
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
                    owner_channels=("api", "local", ""),
                    workspace_dir=workspace,
                    poll_interval_seconds=0.05,
                )
                await supervisor.start()
                self.assertTrue(has_live_job_supervisor(jobs_dir, channel="api"))
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
                self.assertFalse(has_live_job_supervisor(jobs_dir, channel="api"))

        asyncio.run(run_test())

    def test_supervisor_skips_foreign_channel_jobs(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                jobs_dir = Path(tmpdir) / "jobs"
                workspace = Path(tmpdir) / "workspace"
                workspace.mkdir()
                job = enqueue_job(
                    kind="process",
                    command="python3 -c \"print('stolen')\"",
                    jobs_dir=jobs_dir,
                    channel="feishu",
                    target={"chat_id": "oc_x"},
                    user_id="u1",
                    title="Foreign",
                )
                notified = []

                async def notify(record):
                    notified.append(record.job_id)

                foreign = AsyncJobSupervisor(
                    jobs_dir,
                    can_handle=lambda record: record.delivery_channel == "api",
                    can_notify=lambda record: record.delivery_channel == "api",
                    notify=notify,
                    owner_channels=("api",),
                    workspace_dir=workspace,
                    poll_interval_seconds=0.05,
                )
                await foreign.start()
                for _ in range(20):
                    await foreign.tick()
                    await asyncio.sleep(0.02)
                await foreign.stop()

                active = list_job_records(jobs_dir, include_claimed=True)
                self.assertEqual(len(active), 1)
                self.assertEqual(active[0].job_id, job.job_id)
                self.assertEqual(active[0].status, "queued")
                self.assertEqual(notified, [])
                self.assertEqual(list_archived_job_records(jobs_dir), [])

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

    def test_cancel_claimed_before_spawn_does_not_start(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                jobs_dir = Path(tmpdir) / "jobs"
                workspace = Path(tmpdir) / "workspace"
                workspace.mkdir()
                job = enqueue_job(
                    kind="process",
                    command="python3 -c \"open('STARTED','w').write('x'); import time; time.sleep(30)\"",
                    jobs_dir=jobs_dir,
                    channel="api",
                    target={"user_id": "web_user"},
                    user_id="web_user",
                    cwd=str(workspace),
                )
                # Simulate claim + cancel race: rename to claimed, mark claimed, cancel.
                path = jobs_dir / f"{job.job_id}.json"
                claimed_path = path.with_name(f"{path.name}{CLAIM_MARKER}race0001")
                path.rename(claimed_path)
                payload = dict(job.payload)
                payload["status"] = JOB_STATUS_CLAIMED
                _write_json_atomic(claimed_path, payload)
                cancelled = request_job_cancel(jobs_dir, job.job_id)
                self.assertTrue(bool(cancelled.payload.get("cancel_requested")))
                self.assertNotEqual(cancelled.status, "cancelled")

                supervisor = AsyncJobSupervisor(
                    jobs_dir,
                    can_handle=lambda record: True,
                    can_notify=lambda record: False,
                    notify=lambda record: asyncio.sleep(0),
                    workspace_dir=workspace,
                    poll_interval_seconds=0.05,
                    cancel_grace_seconds=0.2,
                )
                await supervisor._run_claimed(
                    claimed_path,
                    get_job(jobs_dir, job.job_id),
                    [],
                )
                archived = list_archived_job_records(jobs_dir)
                self.assertEqual(len(archived), 1)
                self.assertEqual(archived[0].status, "cancelled")
                self.assertFalse((workspace / "STARTED").exists())

        asyncio.run(run_test())

    def test_cancel_running_job_kills_process_group(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                jobs_dir = Path(tmpdir) / "jobs"
                workspace = Path(tmpdir) / "workspace"
                workspace.mkdir()
                # Child sleeps independently of the shell; group kill must reap it.
                job = enqueue_job(
                    kind="process",
                    command=(
                        "python3 -c \"import os, time; "
                        "open('child.pid','w').write(str(os.getpid())); "
                        "time.sleep(60)\""
                    ),
                    jobs_dir=jobs_dir,
                    channel="api",
                    target={"user_id": "web_user"},
                    user_id="web_user",
                    cwd=str(workspace),
                )
                supervisor = AsyncJobSupervisor(
                    jobs_dir,
                    can_handle=lambda record: record.delivery_channel == "api",
                    can_notify=lambda record: False,
                    notify=lambda record: asyncio.sleep(0),
                    owner_channels=("api",),
                    workspace_dir=workspace,
                    poll_interval_seconds=0.05,
                    cancel_grace_seconds=0.2,
                )
                await supervisor.start()
                child_pid = None
                for _ in range(100):
                    pid_file = workspace / "child.pid"
                    if pid_file.is_file():
                        child_pid = int(pid_file.read_text(encoding="utf-8").strip())
                        break
                    await asyncio.sleep(0.05)
                self.assertIsNotNone(child_pid)
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
                # Child must be gone (process-group kill).
                dead = False
                for _ in range(40):
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        dead = True
                        break
                    time.sleep(0.05)
                self.assertTrue(dead, f"child pid {child_pid} still alive after cancel")

        asyncio.run(run_test())

    def test_delete_job_removes_work_and_logs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            job = enqueue_job(
                kind="process",
                command="echo hi",
                jobs_dir=tmpdir,
                channel="api",
                target={"user_id": "web_user"},
                user_id="web_user",
            )
            job_dir = Path(tmpdir) / job.job_id
            (job_dir / "stdout.log").write_text("log\n", encoding="utf-8")
            self.assertTrue(job_dir.is_dir())
            delete_job(tmpdir, job.job_id)
            self.assertFalse(job.path.exists())
            self.assertFalse(job_dir.exists())

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

    def test_enqueue_dedupes_resources(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            job = enqueue_job(
                kind="process",
                command="true",
                jobs_dir=tmpdir,
                channel="api",
                target={},
                resources=["gpu:0", "gpu:0", "disk", "gpu:0"],
            )
            self.assertEqual(job.resources, ["gpu:0", "disk"])

    def test_resource_locks_are_exclusive_across_handles(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            jobs_dir = Path(tmpdir)
            first = _try_acquire_resource_locks(jobs_dir, ["serial:dmx"])
            second = _try_acquire_resource_locks(jobs_dir, ["serial:dmx"])
            self.assertIsNotNone(first)
            self.assertIsNone(second)
            _release_resource_locks(first or [])
            third = _try_acquire_resource_locks(jobs_dir, ["serial:dmx"])
            self.assertIsNotNone(third)
            _release_resource_locks(third or [])

    def test_supervisor_adopts_live_pid_after_restart(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                jobs_dir = Path(tmpdir) / "jobs"
                workspace = Path(tmpdir) / "workspace"
                workspace.mkdir()
                job = enqueue_job(
                    kind="process",
                    command="true",
                    jobs_dir=jobs_dir,
                    channel="api",
                    target={"user_id": "web_user"},
                    user_id="web_user",
                    cwd=str(workspace),
                )
                process = await asyncio.create_subprocess_shell(
                    "python3 -c \"import time; time.sleep(60)\"",
                    cwd=str(workspace),
                    start_new_session=True,
                )
                self.assertIsNotNone(process.pid)
                path = jobs_dir / f"{job.job_id}.json"
                claimed_path = path.with_name(f"{path.name}{CLAIM_MARKER}adopt01")
                path.rename(claimed_path)
                payload = dict(job.payload)
                payload["status"] = JOB_STATUS_RUNNING
                payload["started_at"] = payload["created_at"]
                payload["execution"] = {
                    "pid": process.pid,
                    "pgid": process.pid,
                    "supervisor_pid": 1_000_000 + (os.getpid() % 1000),
                    "claimed_at_ts": time.time() - 60,
                    "started_at": payload["created_at"],
                }
                _write_json_atomic(claimed_path, payload)

                supervisor = AsyncJobSupervisor(
                    jobs_dir,
                    can_handle=lambda record: record.delivery_channel == "api",
                    can_notify=lambda record: False,
                    notify=lambda record: asyncio.sleep(0),
                    owner_channels=("api",),
                    workspace_dir=workspace,
                    poll_interval_seconds=0.05,
                    cancel_grace_seconds=0.2,
                )
                await supervisor.start()
                for _ in range(40):
                    if job.job_id in supervisor._inflight:
                        break
                    await asyncio.sleep(0.05)
                self.assertIn(job.job_id, supervisor._inflight)
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
                self.assertTrue(bool((archived[0].payload.get("execution") or {}).get("adopted")))

        asyncio.run(run_test())

    def test_supervisor_stop_marks_running_job_cancelled(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                jobs_dir = Path(tmpdir) / "jobs"
                workspace = Path(tmpdir) / "workspace"
                workspace.mkdir()
                enqueue_job(
                    kind="process",
                    command="python3 -c \"import time; time.sleep(30)\"",
                    jobs_dir=jobs_dir,
                    channel="api",
                    target={"user_id": "web_user"},
                    user_id="web_user",
                )
                supervisor = AsyncJobSupervisor(
                    jobs_dir,
                    can_handle=lambda record: True,
                    can_notify=lambda record: False,
                    notify=lambda record: asyncio.sleep(0),
                    workspace_dir=workspace,
                    poll_interval_seconds=0.05,
                    cancel_grace_seconds=0.2,
                )
                await supervisor.start()
                for _ in range(80):
                    records = list_job_records(jobs_dir, include_claimed=True)
                    if records and records[0].status == "running":
                        break
                    await asyncio.sleep(0.05)
                await supervisor.stop()
                archived = list_archived_job_records(jobs_dir)
                self.assertEqual(len(archived), 1)
                self.assertEqual(archived[0].status, "cancelled")

        asyncio.run(run_test())

    def test_supervisor_claims_queued_jobs_fifo(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                jobs_dir = Path(tmpdir) / "jobs"
                workspace = Path(tmpdir) / "workspace"
                workspace.mkdir()
                order_file = workspace / "order.txt"
                enqueue_job(
                    kind="process",
                    command="python3 -c \"open('order.txt','a').write('old\\n'); import time; time.sleep(0.2)\"",
                    jobs_dir=jobs_dir,
                    channel="api",
                    target={"user_id": "web_user"},
                    user_id="web_user",
                    cwd=str(workspace),
                    title="old",
                )
                await asyncio.sleep(0.02)
                enqueue_job(
                    kind="process",
                    command="python3 -c \"open('order.txt','a').write('new\\n')\"",
                    jobs_dir=jobs_dir,
                    channel="api",
                    target={"user_id": "web_user"},
                    user_id="web_user",
                    cwd=str(workspace),
                    title="new",
                )
                supervisor = AsyncJobSupervisor(
                    jobs_dir,
                    can_handle=lambda record: True,
                    can_notify=lambda record: False,
                    notify=lambda record: asyncio.sleep(0),
                    workspace_dir=workspace,
                    poll_interval_seconds=0.05,
                    max_concurrent_jobs=1,
                )
                await supervisor.start()
                for _ in range(80):
                    if len(list_archived_job_records(jobs_dir)) >= 2:
                        break
                    await asyncio.sleep(0.05)
                await supervisor.stop()
                self.assertEqual(order_file.read_text(encoding="utf-8"), "old\nnew\n")

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
