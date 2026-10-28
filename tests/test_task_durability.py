"""Fault, recovery and duplicate-delivery regressions for task SQLite."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from xagent.core.runtime.tasks import AsyncTaskScheduler, enqueue_scheduled_task, get_scheduled_task
from xagent.core.runtime.task_store import TaskStore, aware, next_due
from xagent.core.runtime.task_receipts import journal_delivery, note_task_tool

UTC = timezone.utc


class TaskDurabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "tasks"
        self.now = datetime(2026, 6, 1, 10, tzinfo=UTC)
        self.executed, self.sent = [], []

    def enqueue(self, recurrence=None, execution=None):
        return enqueue_scheduled_task(task_type="agent", content="check", run_at=self.now,
            tasks_dir=self.root, channel="api", target={"user_id": "alice"},
            recurrence=recurrence, timezone_name="UTC", execution=execution)

    async def execute(self, task):
        self.executed.append(task.task_id)
        return {"content": "prepared result"}

    async def deliver(self, task, result, run_id):
        self.sent.append((run_id, result["content"]))
        return {"accepted": True}

    def scheduler(self, **kwargs):
        return AsyncTaskScheduler(self.root, can_handle=lambda task: True,
            execute=self.execute, deliver=self.deliver, now_provider=lambda: self.now, **kwargs)

    def runs(self, task):
        return TaskStore(self.root).list_runs(task.task_id)

    async def test_uncertain_delivery_retained_and_tomorrow_still_runs(self):
        task = self.enqueue([{"kind": "daily", "time": "10:00:00"}])
        scheduler = self.scheduler()
        async def unknown(*args):
            raise TimeoutError("connection lost after send")
        scheduler.deliver = unknown
        await scheduler.tick()
        old = self.runs(task)[0]
        self.assertEqual(old["status"], "needs_review")
        self.assertEqual(get_scheduled_task(self.root, task.task_id).status, "active")
        scheduler.deliver = self.deliver
        self.now += timedelta(days=1)
        await scheduler.tick()
        self.assertEqual(len(self.executed), 2)
        self.assertEqual(len(self.sent), 1)
        self.assertIn(old["id"], get_scheduled_task(self.root, task.task_id).payload["unresolved_runs"])
        with self.assertRaisesRegex(ValueError, "unknown"):
            scheduler.store.act_on_run(task.task_id, old["id"], "deliver", self.now - timedelta(seconds=1))

    async def test_safe_preparation_retries_three_times_with_distinct_attempts(self):
        task = self.enqueue()
        scheduler = self.scheduler()
        async def offline(record):
            note_task_tool("read", "read_only")
            raise ConnectionError("offline")
        scheduler.execute = offline
        for delay in (0, 30, 120):
            self.now += timedelta(seconds=delay)
            await scheduler.tick()
        run = scheduler.store.get_run(task.task_id, self.runs(task)[0]["id"])
        self.assertEqual(run["status"], "failed")
        self.assertEqual(len(run["attempts"]), 3)
        self.assertEqual(len({r["id"] for r in run["attempts"]}), 3)
        self.assertEqual(self.sent, [])

    async def test_unknown_tool_is_not_repeated_after_failure(self):
        task = self.enqueue()
        scheduler = self.scheduler()
        async def external(record):
            note_task_tool("unknown_plugin", "external")
            raise ConnectionError("provider failed after tool")
        scheduler.execute = external
        await scheduler.tick()
        self.assertEqual(self.runs(task)[0]["status"], "needs_review")
        with self.assertRaisesRegex(ValueError, "side effects"):
            scheduler.store.act_on_run(task.task_id, self.runs(task)[0]["id"], "prepare", self.now)

    async def test_delivery_retry_reuses_prepared_result(self):
        task = self.enqueue()
        scheduler = self.scheduler()
        async def missing_config(*args):
            raise ValueError("missing recipient config; send has not started")
        scheduler.deliver = missing_config
        await scheduler.tick()
        run = self.runs(task)[0]
        scheduler.store.act_on_run(task.task_id, run["id"], "deliver", self.now)
        scheduler.deliver = self.deliver
        await scheduler.tick()
        self.assertEqual(len(self.executed), 1)
        self.assertEqual(self.sent, [(run["id"], "prepared result")])

    async def test_partial_delivery_retries_upload_without_resending_text(self):
        task = self.enqueue()
        scheduler = self.scheduler()
        calls = []
        async def text():
            calls.append("text")
            return {"message_id": "1"}
        async def upload():
            calls.append("upload")
            if calls.count("upload") == 1:
                raise ConnectionError("CDN down")
            return {"upload_id": "2"}
        async def delivery(*args):
            await journal_delivery("text", text, payload="hello")
            await journal_delivery("upload", upload, uncertain_on_error=False)
            return {"accepted": True}
        scheduler.deliver = delivery
        await scheduler.tick()
        self.assertEqual(self.runs(task)[0]["status"], "retry_wait")
        self.now += timedelta(seconds=30)
        await scheduler.tick()
        self.assertEqual(calls, ["text", "upload", "upload"])
        self.assertEqual(len(self.executed), 1)
        self.assertEqual(self.runs(task)[0]["status"], "succeeded")

    async def test_unacknowledged_part_is_not_retried(self):
        task = self.enqueue()
        scheduler = self.scheduler()
        async def no_ack():
            raise TimeoutError("lost acknowledgement")
        async def delivery(*args):
            await journal_delivery("text", no_ack)
        scheduler.deliver = delivery
        await scheduler.tick()
        self.assertEqual(self.runs(task)[0]["status"], "needs_review")
        run = self.runs(task)[0]
        with self.assertRaises(ValueError):
            scheduler.store.act_on_run(task.task_id, run["id"], "deliver", self.now)

    async def test_atomic_materialization_rolls_back_occurrence_and_next_schedule(self):
        task = self.enqueue([{"kind": "daily", "time": "10:00:00"}])
        store = TaskStore(self.root)
        with patch.object(store, "_insert_run", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.materialize(self.now)
        self.assertEqual(store.list_runs(task.task_id), [])
        self.assertEqual(aware(store.task(task.task_id)["next_due"]), self.now)
        store.materialize(self.now)
        store.materialize(self.now)
        self.assertEqual(len(store.list_runs(task.task_id)), 1)

    async def test_downtime_coalesces_old_occurrences_and_executes_only_latest(self):
        task = self.enqueue([{"kind": "daily", "time": "10:00:00"}])
        self.now += timedelta(days=3, minutes=1)
        await self.scheduler().tick()
        runs = self.runs(task)
        self.assertEqual(len(self.executed), 1)
        self.assertEqual([r["status"] for r in runs], ["succeeded", "skipped"])
        self.assertEqual(runs[1]["skip_count"], 3)
        self.assertEqual(aware(runs[0]["due_at"]), self.now.replace(minute=0))

    async def test_expired_one_shot_is_visible_and_not_executed(self):
        task = self.enqueue(execution={"max_lateness_seconds": 60})
        self.now += timedelta(minutes=2)
        await self.scheduler().tick()
        self.assertEqual(self.executed, [])
        self.assertEqual(self.runs(task)[0]["status"], "skipped")
        self.assertEqual(get_scheduled_task(self.root, task.task_id).status, "completed")

    async def test_unavailable_channel_does_not_consume_execution_attempts(self):
        task = self.enqueue()
        scheduler = self.scheduler()
        scheduler.can_handle = lambda record: False
        await scheduler.tick()
        self.assertNotIn("attempt_id", self.runs(task)[0])
        self.assertEqual(self.runs(task)[0]["status"], "waiting_channel")
        self.now += timedelta(seconds=30)
        scheduler.can_handle = lambda record: True
        await scheduler.tick()
        self.assertEqual(len(self.sent), 1)

    async def test_claim_is_exclusive_and_same_task_never_runs_concurrently(self):
        task = self.enqueue([{"kind": "daily", "time": "10:00:00"}])
        store = TaskStore(self.root)
        store.materialize(self.now)
        self.assertIsNotNone(store.claim(self.now))
        self.now += timedelta(days=1)
        store.materialize(self.now)
        self.assertIsNone(TaskStore(self.root).claim(self.now))

    async def test_restart_recovers_readonly_preparation_but_quarantines_external_tool(self):
        task = self.enqueue()
        store = TaskStore(self.root)
        store.materialize(self.now)
        run = store.claim(self.now)
        store.recover(self.now)
        self.assertEqual(self.runs(task)[0]["status"], "pending")
        run = store.claim(self.now)
        run["unsafe_tools_started"] = True
        store.save_run(run)
        store.recover(self.now)
        self.assertEqual(self.runs(task)[0]["status"], "needs_review")

    async def test_restart_after_accepted_part_resumes_without_generation(self):
        task = self.enqueue()
        store = TaskStore(self.root)
        store.materialize(self.now)
        run = store.claim(self.now)
        run.update(status="delivering", phase="deliver", result={"content": "saved"})
        store.save_run(run)
        store.save_delivery(run["id"], {"id": "text", "status": "accepted", "receipt": {}})
        store.recover(self.now)
        scheduler = self.scheduler()
        await scheduler.tick()
        self.assertEqual(self.executed, [])
        self.assertEqual(self.sent[0][1], "saved")

    async def test_review_resolution_preserves_future_schedule(self):
        task = self.enqueue([{"kind": "daily", "time": "10:00:00"}])
        scheduler = self.scheduler()
        async def unknown(*args):
            raise TimeoutError("unknown")
        scheduler.deliver = unknown
        await scheduler.tick()
        before = scheduler.store.task(task.task_id)["next_due"]
        scheduler.store.act_on_run(task.task_id, self.runs(task)[0]["id"], "resolve", self.now)
        self.assertEqual(scheduler.store.task(task.task_id)["next_due"], before)
        self.assertEqual(scheduler.store.task(task.task_id)["unresolved_runs"], [])
        self.assertEqual(len(self.executed), 1)

    async def test_dependency_policy_blocks_only_until_review_resolved(self):
        task = self.enqueue([{"kind": "daily", "time": "10:00:00"}], {"block_on_uncertain": True})
        scheduler = self.scheduler()
        async def unknown(*args):
            raise TimeoutError("unknown")
        scheduler.deliver = unknown
        await scheduler.tick()
        self.now += timedelta(days=1)
        await scheduler.tick()
        self.assertEqual(len(self.executed), 1)
        scheduler.store.act_on_run(task.task_id, self.runs(task)[0]["id"], "resolve", self.now)
        scheduler.deliver = self.deliver
        await scheduler.tick()
        self.assertEqual(len(self.executed), 2)

    async def test_timezone_is_frozen_and_dst_gap_is_skipped(self):
        task = self.enqueue([{"kind": "daily", "time": "10:00:00"}])
        with patch.dict(os.environ, {"TZ": "America/New_York"}):
            await self.scheduler().tick()
        self.assertEqual(aware(TaskStore(self.root).task(task.task_id)["next_due"]), self.now + timedelta(days=1))
        payload = {"timezone": "America/New_York", "recurrence": [{"kind": "daily", "time": "02:30:00"}]}
        self.assertEqual(next_due(payload, aware("2026-03-07T02:30:00", payload["timezone"])), aware("2026-03-09T02:30:00", payload["timezone"]))

    async def test_dst_fall_back_runs_once(self):
        payload = {"timezone": "America/New_York", "recurrence": [{"kind": "daily", "time": "01:30:00"}]}
        due = aware("2026-11-01T01:30:00", payload["timezone"])
        following = next_due(payload, due)
        self.assertEqual(following, aware("2026-11-02T01:30:00", payload["timezone"]))


    async def test_editing_queued_content_updates_the_snapshot_without_an_extra_run(self):
        from xagent.core.runtime.tasks import update_scheduled_task
        task = self.enqueue()
        store = TaskStore(self.root)
        store.materialize(self.now)
        update_scheduled_task(self.root, task.task_id, content="updated")
        run = store.claim(self.now)
        self.assertEqual(run["snapshot"]["task"]["content"], "updated")
        self.assertIsNone(store.task(task.task_id)["next_due"])
        self.assertEqual(len(store.list_runs(task.task_id)), 1)

    async def test_rescheduling_skips_the_queued_occurrence(self):
        from xagent.core.runtime.tasks import update_scheduled_task
        task = self.enqueue()
        store = TaskStore(self.root)
        store.materialize(self.now)
        update_scheduled_task(self.root, task.task_id, run_at=self.now + timedelta(hours=1))
        self.assertEqual(store.list_runs(task.task_id)[0]["status"], "skipped")
        self.assertIsNone(store.claim(self.now))

    async def test_editing_a_running_task_is_rejected(self):
        from xagent.core.runtime.tasks import update_scheduled_task
        task = self.enqueue()
        store = TaskStore(self.root)
        store.materialize(self.now)
        store.claim(self.now)
        with self.assertRaises(ValueError):
            update_scheduled_task(self.root, task.task_id, content="new")


    async def test_pause_after_last_interval_was_queued_expires_without_a_next_due(self):
        from xagent.core.runtime.tasks import pause_scheduled_task
        task = self.enqueue([{"kind": "interval", "every_seconds": 60, "end_at": self.now.isoformat()}])
        scheduler = self.scheduler()
        scheduler.can_handle = lambda record: False
        await scheduler.tick()
        pause_scheduled_task(self.root, task.task_id)
        self.now += timedelta(minutes=1)
        await scheduler.tick()
        self.assertEqual(get_scheduled_task(self.root, task.task_id).status, "completed")
        self.assertEqual(self.runs(task)[0]["status"], "skipped")

    async def test_process_exit_during_materialization_rolls_back_both_changes(self):
        import subprocess
        import sys
        task = self.enqueue([{"kind": "daily", "time": "10:00:00"}])
        program = """
import os, sys
from xagent.core.runtime.task_store import TaskStore, aware
store = TaskStore(sys.argv[1])
original = store._insert_run
def insert_then_exit(*args, **kwargs):
    original(*args, **kwargs)
    os._exit(9)
store._insert_run = insert_then_exit
store.materialize(aware(sys.argv[2]))
"""
        child = subprocess.run([sys.executable, "-c", program, str(self.root), self.now.isoformat()], capture_output=True)
        self.assertEqual(child.returncode, 9, child.stderr.decode())
        store = TaskStore(self.root)
        self.assertEqual(store.list_runs(task.task_id), [])
        self.assertEqual(aware(store.task(task.task_id)["next_due"]), self.now)

    async def test_process_exit_after_preparation_reuses_durable_result(self):
        import subprocess
        import sys
        task = self.enqueue()
        program = """
import os, sys
from xagent.core.runtime.task_store import TaskStore, aware
store = TaskStore(sys.argv[1]); now = aware(sys.argv[2])
store.materialize(now); run = store.claim(now)
run.update(status='result_ready', phase='deliver', result={'content':'saved before exit'})
store.save_run(run)
os._exit(9)
"""
        child = subprocess.run([sys.executable, "-c", program, str(self.root), self.now.isoformat()], capture_output=True)
        self.assertEqual(child.returncode, 9, child.stderr.decode())
        scheduler = self.scheduler()
        scheduler.recover_running_tasks()
        await scheduler.tick()
        self.assertEqual(self.executed, [])
        self.assertEqual(self.sent[0][1], "saved before exit")


    async def test_initial_calendar_occurrence_skips_dst_gap(self):
        from xagent.core.runtime.tasks import resolve_scheduled_task_run_at
        when, rules = resolve_scheduled_task_run_at(recurrence=[{"kind": "daily", "time": "02:30:00"}],
            now=datetime(2026, 3, 8, 1), timezone_name="America/New_York")
        self.assertEqual(when, datetime(2026, 3, 9, 2, 30))

    async def test_interval_offset_is_interpreted_in_task_timezone(self):
        task = self.enqueue([{"kind": "interval", "every_seconds": 60, "end_at": self.now.isoformat()}])
        self.assertEqual(TaskStore(self.root).task(task.task_id)["recurrence"][0]["end_at"], "2026-06-01 10:00:00")



class TaskCleanStartTests(unittest.TestCase):
    def test_old_json_is_neither_imported_nor_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tasks"
            root.mkdir()
            source = root / "20260601-100000-old.json"
            source.write_text("invalid old JSON")
            self.assertEqual(TaskStore(root).tasks(), [])
            self.assertEqual(source.read_text(), "invalid old JSON")

    def test_unsupported_schema_stops_before_execution(self):
        from xagent.core.runtime.task_store import TaskStorageError
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(tmp)
            with store.transaction() as connection:
                connection.execute("PRAGMA user_version=99")
            with self.assertRaises(TaskStorageError):
                TaskStore(tmp)
