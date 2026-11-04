"""WorkStore persistence and WorkRunner pool/cancel semantics."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from xagent.core.runtime.turns import TurnStore
from xagent.core.runtime.work_runner import WorkControls, WorkRunner
from xagent.core.runtime.work_store import (
    CONTROL_INCORPORATED,
    CONTROL_RECEIVED,
    WorkStore,
    recover_work,
)


def _fields(**overrides):
    fields = dict(channel="api", user_id="alice", room_name="", source="chat",
                  goal="ship the thing", completion_criteria="tests green")
    fields.update(overrides)
    return fields


class WorkStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_get_list_and_scope_filtering(self):
        store = WorkStore()
        record = await store.create(**_fields())
        self.assertEqual(record["status"], "queued")
        self.assertEqual((await store.get(record["id"]))["goal"], "ship the thing")
        self.assertEqual(len(await store.list_for("api", "alice")), 1)
        # Strict channel+user scoping: cross-channel and cross-user lookups see nothing.
        self.assertEqual(await store.list_for("weixin", "alice"), [])
        self.assertEqual(await store.list_for("api", "bob"), [])
        with self.assertRaises(KeyError):
            await store.get("missing")

    async def test_controls_track_received_then_incorporated(self):
        store = WorkStore()
        record = await store.create(**_fields())
        control_id = await store.append_control(record["id"], "also update the docs")
        record = await store.get(record["id"])
        self.assertEqual(record["controls"][0]["state"], CONTROL_RECEIVED)
        self.assertEqual(record["controls"][0]["content"], "also update the docs")
        await store.mark_control_incorporated(record["id"], control_id)
        record = await store.get(record["id"])
        self.assertEqual(record["controls"][0]["state"], CONTROL_INCORPORATED)

    async def test_update_and_controls_survive_a_reopened_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "work.sqlite3"
            store = WorkStore(path)
            record = await store.create(**_fields())
            await store.update(record["id"], status="running")
            await store.append_control(record["id"], "keep going")
            reopened = WorkStore(path)
            record = await reopened.get(record["id"])
            self.assertEqual(record["status"], "running")
            self.assertEqual(record["controls"][0]["content"], "keep going")

    async def test_recover_work_resolves_each_nonterminal_status(self):
        store = WorkStore()
        turn_store = TurnStore()
        await turn_store.begin(event_key="k1", turn_id="t-running")
        await turn_store.update("t-running", resumable=True, side_effect_started=False,
                                resume_snapshot=[{"role": "user", "content": "hi"}])
        await turn_store.begin(event_key="k2", turn_id="t-unsafe")
        await turn_store.update("t-unsafe", resumable=True, side_effect_started=True)
        queued = await store.create(**_fields())
        running = await store.create(**_fields())
        await store.update(running["id"], status="running", turn_ids=["t-running"])
        unsafe = await store.create(**_fields())
        await store.update(unsafe["id"], status="running", turn_ids=["t-unsafe"])
        orphan = await store.create(**_fields())
        await store.update(orphan["id"], status="running", turn_ids=["t-missing"])
        stopping = await store.create(**_fields())
        await store.update(stopping["id"], status="stopping")
        waiting = await store.create(**_fields())
        await store.update(waiting["id"], status="waiting_user")

        recovered = await recover_work(store, turn_store)

        self.assertEqual(recovered, 4)
        self.assertEqual((await store.get(queued["id"]))["status"], "queued")
        resumed = await store.get(running["id"])
        self.assertEqual(resumed["status"], "queued")
        self.assertEqual(resumed["resume_from"], [{"role": "user", "content": "hi"}])
        self.assertEqual((await store.get(unsafe["id"]))["status"], "needs_review")
        self.assertEqual((await store.get(orphan["id"]))["status"], "needs_review")
        self.assertEqual((await store.get(stopping["id"]))["status"], "completed")
        self.assertEqual((await store.get(stopping["id"]))["completion_reason"], "stopped")
        self.assertEqual((await store.get(waiting["id"]))["status"], "waiting_user")


class WorkRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_executes_queued_work_and_completes_it(self):
        store = WorkStore()
        ran = []
        async def execute(record, controls):
            ran.append(record["id"])
            self.assertIsInstance(controls, WorkControls)
        runner = WorkRunner(work_store=store, execute=execute)
        work = await store.create(**_fields())
        self.assertTrue(await runner.submit(work["id"]))
        deadline = asyncio.get_running_loop().time() + 2.0
        while asyncio.get_running_loop().time() < deadline:
            if (await store.get(work["id"]))["status"] == "completed":
                break
            await asyncio.sleep(0.005)
        self.assertEqual(ran, [work["id"]])
        self.assertEqual((await store.get(work["id"]))["status"], "completed")

    async def test_pool_limit_serializes_third_work_item(self):
        store = WorkStore()
        entered = []
        release = asyncio.Event()
        async def execute(record, controls):
            entered.append(record["id"])
            await release.wait()
        runner = WorkRunner(work_store=store, max_background_work=2, execute=execute)
        works = [await store.create(**_fields()) for _ in range(3)]
        for work in works:
            self.assertTrue(await runner.submit(work["id"]))
        deadline = asyncio.get_running_loop().time() + 2.0
        while len(entered) < 2 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.005)
        self.assertEqual(set(entered), {works[0]["id"], works[1]["id"]})
        self.assertEqual((await store.get(works[2]["id"]))["status"], "queued")
        release.set()
        deadline = asyncio.get_running_loop().time() + 2.0
        while asyncio.get_running_loop().time() < deadline:
            statuses = [await store.get(work["id"]) for work in works]
            if all(record["status"] == "completed" for record in statuses):
                break
            await asyncio.sleep(0.005)
        self.assertEqual(set(entered), {work["id"] for work in works})

    async def test_cancel_running_work_completes_as_stopped(self):
        store = WorkStore()
        release = asyncio.Event()
        async def execute(record, controls):
            await controls.cancel_event.wait()
            await store.update(record["id"], status="stopping")  # driver acknowledges
        runner = WorkRunner(work_store=store, execute=execute)
        work = await store.create(**_fields())
        await runner.submit(work["id"])
        deadline = asyncio.get_running_loop().time() + 2.0
        while (await store.get(work["id"]))["status"] != "running" and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.005)
        self.assertTrue(await runner.cancel(work["id"]))
        record = await store.get(work["id"])
        self.assertEqual(record["status"], "stopping")
        release.set()
        deadline = asyncio.get_running_loop().time() + 2.0
        while (await store.get(work["id"]))["status"] != "completed" and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.005)
        record = await store.get(work["id"])
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["completion_reason"], "stopped")

    async def test_cancel_while_waiting_for_a_pool_slot_completes_as_stopped(self):
        store = WorkStore()
        blocker = asyncio.Event()
        async def execute(record, controls):
            await blocker.wait()
        runner = WorkRunner(work_store=store, max_background_work=1, execute=execute)
        first = await store.create(**_fields())
        second = await store.create(**_fields())
        await runner.submit(first["id"])
        await runner.submit(second["id"])
        deadline = asyncio.get_running_loop().time() + 2.0
        while (await store.get(first["id"]))["status"] != "running" and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.005)
        self.assertTrue(await runner.cancel(second["id"]))
        blocker.set()
        deadline = asyncio.get_running_loop().time() + 2.0
        while asyncio.get_running_loop().time() < deadline:
            statuses = [(await store.get(work["id"]))["status"] for work in (first, second)]
            if statuses == ["completed", "completed"]:
                break
            await asyncio.sleep(0.005)
        self.assertEqual((await store.get(second["id"]))["completion_reason"], "stopped")

    async def test_supplement_reaches_running_driver_only(self):
        store = WorkStore()
        received = []
        started = asyncio.Event()
        async def execute(record, controls):
            started.set()
            while True:
                item = await controls.next_supplement()
                if item is None:
                    await asyncio.sleep(0.001)
                    continue
                control_id, content = item
                received.append((control_id, content))
                if len(received) == 2:
                    return
        runner = WorkRunner(work_store=store, execute=execute)
        work = await store.create(**_fields())
        await runner.submit(work["id"])
        await started.wait()
        self.assertTrue(await runner.supplement(work["id"], "c1", "first"))
        self.assertTrue(await runner.supplement(work["id"], "c2", "second"))
        deadline = asyncio.get_running_loop().time() + 2.0
        while (await store.get(work["id"]))["status"] != "completed" and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.005)
        self.assertEqual(received, [("c1", "first"), ("c2", "second")])
        # Once the run finished, no control channel exists anymore.
        self.assertFalse(await runner.supplement(work["id"], "c3", "late"))

    async def test_execute_failure_marks_work_failed(self):
        store = WorkStore()
        async def execute(record, controls):
            raise ValueError("bad plan")
        runner = WorkRunner(work_store=store, execute=execute)
        work = await store.create(**_fields())
        await runner.submit(work["id"])
        deadline = asyncio.get_running_loop().time() + 2.0
        while (await store.get(work["id"]))["status"] != "failed" and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.005)
        self.assertEqual((await store.get(work["id"]))["status"], "failed")

    async def test_submit_rejects_duplicates_and_nonqueued_work(self):
        store = WorkStore()
        async def execute(record, controls):
            await asyncio.Event().wait()
        runner = WorkRunner(work_store=store, execute=execute)
        work = await store.create(**_fields())
        self.assertTrue(await runner.submit(work["id"]))
        self.assertFalse(await runner.submit(work["id"]))
        self.assertFalse(await runner.submit("missing"))
        done = await store.create(**_fields())
        await store.update(done["id"], status="completed")
        self.assertFalse(await runner.submit(done["id"]))


if __name__ == "__main__":
    unittest.main()
