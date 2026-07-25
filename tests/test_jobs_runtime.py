"""Reliability tests for the transactional background-job runtime."""
from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import signal
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from xagent.core.runtime import (
    AsyncJobDeliveryDispatcher,
    IdempotencyConflict,
    JobSettings,
    JobStore,
    ScheduledDeliveryContext,
    ensure_worker_running,
    scheduled_delivery_context,
)
from xagent.core.runtime.job_process import pid_is_running, signal_verified_process_group
from xagent.core.runtime.job_store import now_ms
from xagent.tools.jobs_tool import create_manage_jobs_tool


ACTIVE = {"queued", "starting", "running", "cancelling"}


def make_settings(**overrides) -> JobSettings:
    values = {
        "worker_idle_seconds": 0.5,
        "runner_heartbeat_seconds": 0.1,
        "runner_stale_seconds": 1.0,
        "cancel_grace_seconds": 0.2,
    }
    values.update(overrides)
    return JobSettings(**values)


def wait_terminal(store: JobStore, job_id: str, timeout: float = 6.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = store.get_job(job_id)
        if record.status not in ACTIVE:
            return record
        time.sleep(0.03)
    raise AssertionError(f"job {job_id} remained {store.get_job(job_id).status}")


def create_idempotent_job_in_process(arguments: tuple[str, str]) -> str:
    jobs_dir, workspace_dir = arguments
    return JobStore(jobs_dir, workspace_dir=workspace_dir).create_job(
        argv=["true"],
        channel="api",
        target={},
        idempotency_scope="process-test",
        idempotency_key="same",
    ).job_id


class JobStoreTests(unittest.TestCase):
    def test_settings_reject_unknown_and_unsafe_heartbeat_values(self):
        with self.assertRaisesRegex(ValueError, "Unsupported jobs key"):
            JobSettings.from_mapping({"unknown": 1})
        with self.assertRaisesRegex(ValueError, "must be an integer"):
            JobSettings.from_mapping({"max_concurrent": "2"})
        with self.assertRaisesRegex(ValueError, "must be a number"):
            JobSettings.from_mapping({"worker_idle_seconds": "60"})
        with self.assertRaisesRegex(ValueError, "must exceed"):
            JobSettings.from_mapping(
                {
                    "runner_heartbeat_seconds": 5,
                    "runner_stale_seconds": 5,
                }
            )

    def test_database_is_single_source_and_legacy_json_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            legacy = root / "legacy.json"
            legacy_failed = root / "failed" / "old.json.failed"
            legacy_failed.parent.mkdir(parents=True)
            legacy.write_text('{"status":"queued"}', encoding="utf-8")
            legacy_failed.write_text('{"status":"failed"}', encoding="utf-8")
            store = JobStore(root)
            with sqlite3.connect(store.db_path) as connection:
                mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(str(mode).lower(), "delete")
            self.assertEqual(version, 1)
            self.assertTrue(legacy.is_file())
            self.assertTrue(legacy_failed.is_file())

    def test_concurrent_idempotent_create_returns_one_job(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JobStore(tmpdir)

            def create():
                return store.create_job(
                    argv=["true"],
                    channel="api",
                    target={},
                    idempotency_scope="test",
                    idempotency_key="same",
                ).job_id

            with ThreadPoolExecutor(max_workers=8) as pool:
                ids = list(pool.map(lambda _index: create(), range(16)))
            self.assertEqual(len(set(ids)), 1)
            records, total = store.list_jobs(scope="all")
            self.assertEqual(total, 1)
            self.assertEqual(records[0].job_id, ids[0])

    def test_multiprocess_idempotent_create_returns_one_job(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            jobs_dir = root / "jobs"
            workspace_dir = root / "workspace"
            JobStore(jobs_dir, workspace_dir=workspace_dir)
            arguments = [(str(jobs_dir), str(workspace_dir))] * 8
            with ProcessPoolExecutor(
                max_workers=4,
                mp_context=multiprocessing.get_context("spawn"),
            ) as pool:
                ids = list(pool.map(create_idempotent_job_in_process, arguments))
            self.assertEqual(len(set(ids)), 1)
            self.assertEqual(
                JobStore(jobs_dir, workspace_dir=workspace_dir).counts()["queued"],
                1,
            )

    def test_idempotency_conflict_rejects_different_spec(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JobStore(tmpdir)
            store.create_job(
                argv=["true"],
                channel="api",
                target={},
                idempotency_scope="test",
                idempotency_key="same",
            )
            with self.assertRaises(IdempotencyConflict):
                store.create_job(
                    argv=["false"],
                    channel="api",
                    target={},
                    idempotency_scope="test",
                    idempotency_key="same",
                )
            with self.assertRaises(IdempotencyConflict):
                store.create_job(
                    argv=["true"],
                    title="different title",
                    channel="api",
                    target={},
                    idempotency_scope="test",
                    idempotency_key="same",
                )

    def test_shell_requires_explicit_opt_in_and_cwd_stays_in_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            store = JobStore(root / "jobs", workspace_dir=workspace)
            with self.assertRaisesRegex(ValueError, "shell=true"):
                store.create_job(command="echo no", channel="api", target={})
            with self.assertRaisesRegex(ValueError, "inside the agent workspace"):
                store.create_job(
                    command="echo no",
                    shell=True,
                    cwd=str(root.parent),
                    channel="api",
                    target={},
                )

    def test_queued_cancel_is_terminal_and_delete_requires_terminal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JobStore(tmpdir)
            active = store.create_job(argv=["true"], channel="api", target={})
            with self.assertRaisesRegex(ValueError, "active"):
                store.delete_job(active.job_id)
            cancelled = store.request_cancel(active.job_id)
            self.assertEqual(cancelled.status, "cancelled")
            self.assertEqual(store.counts()["history"], 1)
            store.delete_job(active.job_id)
            with self.assertRaises(FileNotFoundError):
                store.get_job(active.job_id)

    def test_claim_and_cancel_race_never_loses_cancellation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JobStore(tmpdir)
            job = store.create_job(argv=["true"], channel="api", target={})
            barrier = threading.Barrier(2)

            def claim():
                barrier.wait()
                return store.claim_next(worker_token="worker")

            def cancel():
                barrier.wait()
                return store.request_cancel(job.job_id)

            with ThreadPoolExecutor(max_workers=2) as pool:
                claim_future = pool.submit(claim)
                cancel_future = pool.submit(cancel)
                claimed = claim_future.result()
                cancel_future.result()
            if claimed is not None:
                store.mark_stale_attempt_terminal(
                    claimed.attempt_id,
                    runner_token=claimed.runner_token,
                    cancelled=True,
                )
            self.assertEqual(store.get_job(job.job_id).status, "cancelled")

    def test_retry_creates_new_linked_job(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            settings = make_settings()
            store = JobStore(root / "jobs", workspace_dir=root / "workspace", settings=settings)
            failed = store.create_job(argv=["false"], channel="api", target={})
            ensure_worker_running(store.root, workspace_dir=store.workspace_dir, settings=settings)
            failed = wait_terminal(store, failed.job_id)
            self.assertEqual(failed.status, "failed")
            retried = store.retry_job(failed.job_id)
            self.assertNotEqual(retried.job_id, failed.job_id)
            self.assertEqual(retried.data["retry_of"], failed.job_id)


class JobExecutionTests(unittest.TestCase):
    def test_worker_runs_argv_and_does_not_inherit_secrets(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            settings = make_settings()
            store = JobStore(root / "jobs", workspace_dir=root / "workspace", settings=settings)
            os.environ["XAGENT_TEST_SECRET"] = "must-not-leak"
            try:
                job = store.create_job(
                    argv=[
                        "python",
                        "-c",
                        "import os; print(os.getenv('XAGENT_TEST_SECRET', 'clean'))",
                    ],
                    channel="api",
                    target={},
                )
                worker = ensure_worker_running(
                    store.root,
                    workspace_dir=store.workspace_dir,
                    settings=settings,
                )
                self.assertTrue(worker.available, worker.warning)
                terminal = wait_terminal(store, job.job_id)
            finally:
                os.environ.pop("XAGENT_TEST_SECRET", None)
            self.assertEqual(terminal.status, "succeeded")
            self.assertEqual(terminal.to_job_view(log_tail=True)["stdout_tail"].strip(), "clean")

    def test_running_cancel_is_cancelling_until_process_reaped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            settings = make_settings()
            store = JobStore(root / "jobs", workspace_dir=root / "workspace", settings=settings)
            job = store.create_job(
                argv=["python", "-c", "import time; time.sleep(30)"],
                channel="api",
                target={},
            )
            ensure_worker_running(store.root, workspace_dir=store.workspace_dir, settings=settings)
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and store.get_job(job.job_id).status != "running":
                time.sleep(0.02)
            self.assertEqual(store.get_job(job.job_id).status, "running")
            response = store.request_cancel(job.job_id)
            self.assertEqual(response.status, "cancelling")
            terminal = wait_terminal(store, job.job_id)
            self.assertEqual(terminal.status, "cancelled")

    def test_uncooperative_process_is_force_killed_after_grace_period(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            settings = make_settings(cancel_grace_seconds=0.2)
            store = JobStore(root / "jobs", workspace_dir=root / "workspace", settings=settings)
            job = store.create_job(
                argv=[
                    "python",
                    "-c",
                    (
                        "import signal,time; "
                        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                        "print('ready', flush=True); time.sleep(30)"
                    ),
                ],
                channel="api",
                target={},
            )
            ensure_worker_running(store.root, workspace_dir=store.workspace_dir, settings=settings)
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                current = store.get_job(job.job_id)
                if "ready" in current.to_job_view(log_tail=True).get("stdout_tail", ""):
                    break
                time.sleep(0.02)
            started_cancel = time.monotonic()
            response = store.request_cancel(job.job_id)
            self.assertEqual(response.status, "cancelling")
            terminal = wait_terminal(store, job.job_id)
            self.assertEqual(terminal.status, "cancelled")
            self.assertLess(time.monotonic() - started_cancel, 1.5)

    def test_timeout_is_failed_and_deadline_is_persisted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            settings = make_settings()
            store = JobStore(root / "jobs", workspace_dir=root / "workspace", settings=settings)
            job = store.create_job(
                argv=["python", "-c", "import time; time.sleep(30)"],
                timeout_seconds=1,
                channel="api",
                target={},
            )
            ensure_worker_running(store.root, workspace_dir=store.workspace_dir, settings=settings)
            terminal = wait_terminal(store, job.job_id)
            self.assertEqual(terminal.status, "failed")
            self.assertEqual(terminal.data["reason"], "timeout")
            self.assertIsNotNone(terminal.data["deadline_at_ms"])

    def test_resource_jobs_preserve_fifo_at_single_concurrency(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            settings = make_settings(max_concurrent=1)
            store = JobStore(root / "jobs", workspace_dir=workspace, settings=settings)
            output = workspace / "order.txt"
            first = store.create_job(
                argv=["python", "-c", "open('order.txt','a').write('old\\n')"],
                resources=["build"],
                channel="api",
                target={},
            )
            second = store.create_job(
                argv=["python", "-c", "open('order.txt','a').write('new\\n')"],
                resources=["build"],
                channel="api",
                target={},
            )
            ensure_worker_running(store.root, workspace_dir=workspace, settings=settings)
            self.assertEqual(wait_terminal(store, first.job_id).status, "succeeded")
            self.assertEqual(wait_terminal(store, second.job_id).status, "succeeded")
            self.assertEqual(output.read_text(encoding="utf-8"), "old\nnew\n")

    def test_logs_rotate_without_rewriting_active_segments(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            settings = make_settings(log_segment_bytes=1024, log_segments=2)
            store = JobStore(root / "jobs", workspace_dir=root / "workspace", settings=settings)
            job = store.create_job(
                argv=["python", "-c", "print('x' * 5000); print('END')"],
                channel="api",
                target={},
            )
            ensure_worker_running(store.root, workspace_dir=store.workspace_dir, settings=settings)
            terminal = wait_terminal(store, job.job_id)
            attempt_dir = terminal.log_dir / str(terminal.attempt["id"])
            segments = sorted(attempt_dir.glob("stdout.*.log"))
            self.assertLessEqual(len(segments), 2)
            self.assertTrue(all(path.stat().st_size <= 1024 for path in segments))
            self.assertIn("END", terminal.to_job_view(log_tail=True)["stdout_tail"])

    def test_atomic_receipt_recovers_without_reexecution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = JobStore(root / "jobs", workspace_dir=root / "workspace")
            job = store.create_job(argv=["true"], channel="api", target={})
            claim = store.claim_next(worker_token="worker")
            self.assertIsNotNone(claim)
            assert claim is not None
            receipt = store.root / job.job_id / claim.attempt_id / "result.json"
            receipt.parent.mkdir(parents=True)
            receipt.write_text(
                json.dumps(
                    {
                        "state": "succeeded",
                        "reason": "exit_zero",
                        "result": {"summary": "Recovered"},
                        "exit_code": 0,
                        "exit_signal": None,
                        "last_error": None,
                        "ended_at_ms": now_ms(),
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(store.reconcile_receipts(), 1)
            self.assertEqual(store.get_job(job.job_id).status, "succeeded")

    def test_worker_crash_does_not_duplicate_or_stop_runner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            settings = make_settings(runner_stale_seconds=2)
            store = JobStore(root / "jobs", workspace_dir=root / "workspace", settings=settings)
            marker = store.workspace_dir / "runs.txt"
            job = store.create_job(
                argv=[
                    "python",
                    "-c",
                    "import time; open('runs.txt','a').write('once\\n'); time.sleep(.6)",
                ],
                channel="api",
                target={},
            )
            worker = ensure_worker_running(
                store.root,
                workspace_dir=store.workspace_dir,
                settings=settings,
            )
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and store.get_job(job.job_id).status != "running":
                time.sleep(0.02)
            self.assertEqual(store.get_job(job.job_id).status, "running")
            assert worker.pid is not None
            os.kill(worker.pid, signal.SIGKILL)
            terminal = wait_terminal(store, job.job_id)
            self.assertEqual(terminal.status, "succeeded")
            self.assertEqual(marker.read_text(encoding="utf-8"), "once\n")

    def test_timeout_deadline_survives_worker_restart(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            settings = make_settings(runner_stale_seconds=2)
            store = JobStore(root / "jobs", workspace_dir=root / "workspace", settings=settings)
            job = store.create_job(
                argv=["python", "-c", "import time; time.sleep(30)"],
                timeout_seconds=1,
                channel="api",
                target={},
            )
            worker = ensure_worker_running(
                store.root,
                workspace_dir=store.workspace_dir,
                settings=settings,
            )
            deadline = time.monotonic() + 4
            running = store.get_job(job.job_id)
            while time.monotonic() < deadline and running.status != "running":
                time.sleep(0.02)
                running = store.get_job(job.job_id)
            original_deadline = running.data["deadline_at_ms"]
            assert worker.pid is not None
            os.kill(worker.pid, signal.SIGKILL)
            replacement = ensure_worker_running(
                store.root,
                workspace_dir=store.workspace_dir,
                settings=settings,
            )
            self.assertTrue(replacement.available, replacement.warning)
            terminal = wait_terminal(store, job.job_id)
            self.assertEqual(terminal.status, "failed")
            self.assertEqual(terminal.data["reason"], "timeout")
            self.assertEqual(terminal.data["deadline_at_ms"], original_deadline)

    def test_runner_crash_is_interrupted_and_child_is_stopped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            settings = make_settings(runner_stale_seconds=0.6)
            store = JobStore(root / "jobs", workspace_dir=root / "workspace", settings=settings)
            job = store.create_job(
                argv=["python", "-c", "import time; time.sleep(30)"],
                channel="api",
                target={},
            )
            ensure_worker_running(store.root, workspace_dir=store.workspace_dir, settings=settings)
            deadline = time.monotonic() + 4
            running = store.get_job(job.job_id)
            while time.monotonic() < deadline and running.status != "running":
                time.sleep(0.02)
                running = store.get_job(job.job_id)
            self.assertEqual(running.status, "running")
            runner_pid = int(running.attempt["runner_pid"])
            child_pid = int(running.attempt["child_pid"])
            os.kill(runner_pid, signal.SIGKILL)
            terminal = wait_terminal(store, job.job_id, timeout=5)
            self.assertEqual(terminal.status, "interrupted")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and pid_is_running(child_pid):
                time.sleep(0.05)
            self.assertFalse(pid_is_running(child_pid))

    def test_stale_attempt_becomes_interrupted_not_succeeded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JobStore(tmpdir)
            job = store.create_job(argv=["true"], channel="api", target={})
            claim = store.claim_next(worker_token="worker")
            assert claim is not None
            terminal = store.mark_stale_attempt_terminal(
                claim.attempt_id,
                runner_token=claim.runner_token,
                cancelled=False,
            )
            self.assertEqual(terminal.status, "interrupted")
            self.assertEqual(terminal.data["reason"], "runner_lost")


class JobDeliveryAndToolTests(unittest.TestCase):
    def test_outbox_retries_and_survives_dispatcher_restart(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                store = JobStore(tmpdir)
                job = store.create_job(argv=["true"], channel="api", target={"user_id": "u"})
                store.request_cancel(job.job_id)
                calls = []

                async def notify(record):
                    calls.append(record.job_id)
                    if len(calls) == 1:
                        raise RuntimeError("temporary")

                first = AsyncJobDeliveryDispatcher(
                    tmpdir,
                    channels=("api",),
                    can_notify=lambda _record: True,
                    notify=notify,
                )
                self.assertEqual(await first.tick(), 0)
                with sqlite3.connect(store.db_path) as connection:
                    connection.execute("UPDATE deliveries SET next_attempt_at_ms = 0")
                    connection.commit()
                second = AsyncJobDeliveryDispatcher(
                    tmpdir,
                    channels=("api",),
                    can_notify=lambda _record: True,
                    notify=notify,
                )
                self.assertEqual(await second.tick(), 1)
                view = store.get_job(job.job_id).to_job_view()
                self.assertEqual(view["deliveries"][0]["state"], "delivered")
                self.assertEqual(calls, [job.job_id, job.job_id])

        asyncio.run(run_test())

    def test_voice_delivery_expires_instead_of_speaking_stale_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JobStore(tmpdir)
            job = store.create_job(argv=["true"], channel="voice", target={})
            store.request_cancel(job.job_id)
            with sqlite3.connect(store.db_path) as connection:
                connection.execute("UPDATE deliveries SET expires_at_ms = 0")
                connection.commit()
            self.assertEqual(store.claim_deliveries(("voice",)), [])
            delivery = store.get_job(job.job_id).to_job_view()["deliveries"][0]
            self.assertEqual(delivery["state"], "expired")

    def test_manage_jobs_tool_uses_delivery_context_and_independent_worker(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                settings = make_settings()
                tool = create_manage_jobs_tool(
                    jobs_dir=str(root / "jobs"),
                    workspace_dir=str(root / "workspace"),
                    settings=settings,
                )
                with scheduled_delivery_context(
                    ScheduledDeliveryContext(
                        channel="api",
                        user_id="web_user",
                        target={"user_id": "web_user"},
                        metadata={"source": "test"},
                    )
                ):
                    result = await tool(
                        action="start",
                        argv=["python", "-c", "print('tool')"],
                        title="Tool job",
                    )
                self.assertTrue(result["ok"], result)
                self.assertTrue(result["worker"]["available"])
                store = JobStore(
                    root / "jobs",
                    workspace_dir=root / "workspace",
                    settings=settings,
                )
                terminal = await asyncio.to_thread(wait_terminal, store, result["job"]["job_id"])
                self.assertEqual(terminal.status, "succeeded")
                listed = await tool(action="list", scope="history")
                self.assertEqual(listed["total"], 1)

        asyncio.run(run_test())

    def test_process_identity_mismatch_fails_closed(self):
        with patch("xagent.core.runtime.job_process.process_identity_matches", return_value=False):
            with patch("xagent.core.runtime.job_process.os.killpg") as killpg:
                sent = signal_verified_process_group(
                    os.getpid(),
                    expected_boot_id="wrong",
                    expected_start_identity="wrong",
                    sig=9,
                )
        self.assertFalse(sent)
        killpg.assert_not_called()


if __name__ == "__main__":
    unittest.main()
