import asyncio
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from xagent.core.runtime.tasks import (
    AsyncTaskScheduler, enqueue_scheduled_task, list_task_records,
    list_archived_task_records, retry_scheduled_task,
)
from xagent.core.runtime.task_receipts import occurrence_run_id


class _Crash(BaseException):
    pass


class TaskDurabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.now = datetime(2026, 6, 1, 10)
        self.executed = []
        self.delivered = []

    async def asyncTearDown(self):
        self.temporary.cleanup()

    def enqueue(self, recurrence=None):
        return enqueue_scheduled_task(task_type="agent", content="check", run_at=self.now,
                                      tasks_dir=self.root / "tasks", channel="api",
                                      target={"user_id": "alice"}, recurrence=recurrence)

    async def execute(self, task):
        self.executed.append(task.task_id)
        return {"content": "done", "attachments": []}

    async def deliver(self, task, result, run_id):
        self.delivered.append((run_id, result["content"]))
        return {"accepted": True, "message_id": "remote-1"}

    def scheduler(self, **kwargs):
        return AsyncTaskScheduler(self.root / "tasks", can_handle=lambda task: True,
                                  execute=self.execute, deliver=self.deliver,
                                  receipts_dir=self.root / ".runtime" / "task_runs",
                                  now_provider=lambda: self.now, **kwargs)

    async def crash_at(self, scheduler, stage):
        original = scheduler._persist_receipt
        def persist(path, record, receipt):
            original(path, record, receipt)
            if receipt["stage"] == stage:
                raise _Crash()
        with patch.object(scheduler, "_persist_receipt", side_effect=persist):
            await scheduler.tick()

    async def test_result_ready_recovery_delivers_saved_result_without_generation(self):
        self.enqueue()
        await self.crash_at(self.scheduler(), "result_ready")
        recovered = self.scheduler()
        self.assertEqual(recovered.recover_running_tasks(), 1)
        await recovered.tick()
        self.assertEqual(len(self.executed), 1)
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(len(list_archived_task_records(self.root / "tasks")), 1)

    async def test_executing_and_delivering_recover_to_review_without_replay(self):
        for stage in ("executing", "delivering"):
            with self.subTest(stage=stage):
                task = self.enqueue()
                scheduler = self.scheduler()
                await self.crash_at(scheduler, stage)
                before = (len(self.executed), len(self.delivered))
                recovered = self.scheduler()
                recovered.recover_running_tasks()
                await recovered.tick()
                record = next(record for record in list_task_records(self.root / "tasks")
                              if record.task_id == task.task_id)
                self.assertEqual(record.status, "needs_review")
                self.assertEqual((len(self.executed), len(self.delivered)), before)

    async def test_success_before_archive_only_reconciles_completion(self):
        self.enqueue()
        await self.crash_at(self.scheduler(), "succeeded")
        recovered = self.scheduler()
        recovered.recover_running_tasks()
        await recovered.tick()
        self.assertEqual(len(self.executed), 1)
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(len(list_archived_task_records(self.root / "tasks")), 1)

    async def test_retry_after_archive_failure_never_redelivers_confirmed_result(self):
        task = self.enqueue()
        scheduler = self.scheduler()
        with patch("xagent.core.runtime.tasks._move_task_to_archive", side_effect=OSError("disk full")):
            await scheduler.tick()
        run_id = occurrence_run_id(task.task_id, task.run_at)
        before = scheduler.receipts.read(run_id)
        self.assertEqual(before["stage"], "succeeded")
        self.assertEqual(list_task_records(self.root / "tasks")[0].status, "failed")
        retry_scheduled_task(self.root / "tasks", task.task_id)
        with patch("xagent.core.runtime.tasks._move_task_to_archive", side_effect=OSError("still full")):
            await scheduler.tick()
        self.assertEqual(scheduler.receipts.read(run_id)["stage"], "succeeded")
        retry_scheduled_task(self.root / "tasks", task.task_id)
        await scheduler.tick()
        self.assertEqual(scheduler.receipts.read(run_id)["attempt_id"], before["attempt_id"])
        self.assertEqual(len(self.executed), 1)
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(len(list_archived_task_records(self.root / "tasks")), 1)

    async def test_recovery_archive_failure_keeps_success_receipt_for_retry(self):
        task = self.enqueue()
        scheduler = self.scheduler()
        await self.crash_at(scheduler, "succeeded")
        recovered = self.scheduler()
        with patch("xagent.core.runtime.tasks._move_task_to_archive", side_effect=OSError("disk full")):
            recovered.recover_running_tasks()
        run_id = occurrence_run_id(task.task_id, task.run_at)
        self.assertEqual(recovered.receipts.read(run_id)["stage"], "succeeded")
        retry_scheduled_task(self.root / "tasks", task.task_id)
        await recovered.tick()
        self.assertEqual(len(self.executed), 1)
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(len(list_archived_task_records(self.root / "tasks")), 1)

    async def test_crash_after_archive_link_leaves_only_one_archive(self):
        task = self.enqueue()
        def linked_then_crashed(path, root, completed_at, *, task_id):
            archive = root / "archive" / completed_at.strftime("%Y-%m")
            archive.mkdir(parents=True)
            os.link(path, archive / f"{completed_at.strftime('%Y%m%d-%H%M%S')}-{task_id[:8]}.json")
            raise _Crash()
        with patch("xagent.core.runtime.tasks._move_task_to_archive", side_effect=linked_then_crashed):
            await self.scheduler().tick()
        recovered = self.scheduler()
        recovered.recover_running_tasks()
        self.assertEqual(len(list_archived_task_records(self.root / "tasks")), 1)
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(list_task_records(self.root / "tasks"), [])

    async def test_crash_after_recurrence_link_keeps_one_next_occurrence(self):
        task = self.enqueue(recurrence=[{"kind": "daily", "time": "10:00:00"}])
        def linked_then_crashed(path, root, run_at, *, task_id):
            os.link(path, root / f"{run_at.strftime('%Y%m%d-%H%M%S')}-{task_id[:8]}.json")
            raise _Crash()
        with patch("xagent.core.runtime.tasks._move_running_task", side_effect=linked_then_crashed):
            await self.scheduler().tick()
        recovered = self.scheduler()
        recovered.recover_running_tasks()
        records = list_task_records(self.root / "tasks")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].run_at, datetime(2026, 6, 2, 10))
        self.now = records[0].run_at
        await recovered.tick()
        self.assertEqual(len(self.executed), 2)
        self.assertEqual(len({item[0] for item in self.delivered}), 2)

    async def test_legacy_running_task_requires_review(self):
        task = self.enqueue()
        task.path.rename(task.path.with_name(task.path.name + ".running-old"))
        scheduler = self.scheduler()
        scheduler.recover_running_tasks()
        await scheduler.tick()
        records = list_task_records(self.root / "tasks")
        self.assertEqual(records[0].status, "needs_review")
        self.assertEqual(self.executed, [])

    async def test_explicit_retry_reuses_result_with_new_attempt(self):
        task = self.enqueue()
        scheduler = self.scheduler()
        await self.crash_at(scheduler, "delivering")
        scheduler.recover_running_tasks()
        run_id = occurrence_run_id(task.task_id, task.run_at)
        old = scheduler.receipts.read(run_id)
        retry_scheduled_task(self.root / "tasks", task.task_id)
        await scheduler.tick()
        new = scheduler.receipts.read(run_id)
        self.assertNotEqual(old["attempt_id"], new["attempt_id"])
        self.assertEqual(new["run_id"], run_id)
        self.assertEqual(len(self.executed), 1)
        self.assertEqual(len(self.delivered), 1)

    async def test_unconfirmed_delivery_blocks_recurring_task(self):
        self.enqueue(recurrence=[{"kind": "daily", "time": "10:00:00"}])
        async def uncertain(task, result, run_id):
            raise TimeoutError("network lost after send")
        scheduler = self.scheduler()
        scheduler.deliver = uncertain
        await scheduler.tick()
        self.now = datetime(2026, 6, 3, 10)
        await scheduler.tick()
        records = list_task_records(self.root / "tasks")
        self.assertEqual(records[0].status, "needs_review")
        self.assertEqual(len(self.executed), 1)

    async def test_bounded_shutdown_marks_interrupted_execution_review(self):
        self.enqueue()
        started = asyncio.Event()
        async def blocked(task):
            started.set()
            await asyncio.Event().wait()
        scheduler = self.scheduler(shutdown_timeout_seconds=0.01)
        scheduler.execute = blocked
        await scheduler.start()
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(scheduler.stop(), 1)
        self.assertEqual(list_task_records(self.root / "tasks")[0].status, "needs_review")
