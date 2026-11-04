"""Agent-facing work tools: delegate, list, cancel — scoped to the caller."""
import unittest

from xagent.core.runtime.work_runner import WorkRunner
from xagent.core.runtime.work_store import WorkStore
from xagent.core.tooling.scope import tool_call_scope
from xagent.tools.work_tools import (
    create_cancel_work_tool,
    create_delegate_work_tool,
    create_list_work_tool,
)


def _scope(user_id="alice", channel="api", room_name=""):
    token = tool_call_scope.set((user_id, channel, room_name, "user_turn"))
    return token


class WorkToolsTests(unittest.IsolatedAsyncioTestCase):
    async def _delegate_tool(self, store=None, runner=None):
        executed = []
        store = store or WorkStore()
        if runner is None:
            async def execute(record, controls):
                executed.append(record["id"])
            runner = WorkRunner(work_store=store, execute=execute)
        return create_delegate_work_tool(store, runner), store, executed

    async def test_delegate_creates_scoped_queued_work_and_submits_it(self):
        token = _scope()
        try:
            tool, store, executed = await self._delegate_tool()
            result = await tool("整理最近的日志并写一份摘要")
        finally:
            tool_call_scope.reset(token)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "queued")
        record = await store.get(result["work_id"])
        self.assertEqual(record["source"], "delegated")
        self.assertEqual(record["channel"], "api")
        self.assertEqual(record["user_id"], "alice")
        self.assertEqual(record["goal"], "整理最近的日志并写一份摘要")
        # The runner executed the submitted work to completion.
        import asyncio
        deadline = asyncio.get_running_loop().time() + 2.0
        while not executed and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.005)
        self.assertEqual(executed, [result["work_id"]])
        self.assertEqual((await store.get(result["work_id"]))["status"], "completed")

    async def test_delegate_without_a_driver_reports_unavailable(self):
        store = WorkStore()
        runner = WorkRunner(work_store=store, execute=None)
        token = _scope()
        try:
            result = await create_delegate_work_tool(store, runner)("do it")
        finally:
            tool_call_scope.reset(token)
        self.assertFalse(result["ok"])

    async def test_delegate_without_a_conversation_scope_refuses(self):
        tool, store, _ = await self._delegate_tool()
        result = await tool("do it")  # no scope set
        self.assertFalse(result["ok"])

    async def test_list_returns_only_the_callers_work_newest_first(self):
        store = WorkStore()
        mine = await store.create(channel="api", user_id="alice", source="delegated", goal="first")
        await store.create(channel="api", user_id="bob", source="delegated", goal="foreign")
        await store.create(channel="weixin", user_id="alice", source="delegated", goal="cross-channel")
        token = _scope()
        try:
            result = await create_list_work_tool(store)()
        finally:
            tool_call_scope.reset(token)
        self.assertTrue(result["ok"])
        self.assertEqual([work["work_id"] for work in result["work"]], [mine["id"]])
        self.assertEqual(result["work"][0]["goal"], "first")
        self.assertEqual(result["work"][0]["status"], "queued")

    async def test_cancel_stops_the_callers_work_only(self):
        store = WorkStore()
        async def execute(record, controls):
            await controls.cancel_event.wait()
        runner = WorkRunner(work_store=store, execute=execute)
        mine = await store.create(channel="api", user_id="alice", source="delegated", goal="mine")
        foreign = await store.create(channel="api", user_id="bob", source="delegated", goal="foreign")
        await runner.submit(mine["id"])
        await runner.submit(foreign["id"])
        token = _scope()
        try:
            own = await create_cancel_work_tool(store, runner)(mine["id"])
            other = await create_cancel_work_tool(store, runner)(foreign["id"])
            missing = await create_cancel_work_tool(store, runner)("nope")
        finally:
            tool_call_scope.reset(token)
        self.assertTrue(own["ok"])
        self.assertTrue(own["stopping"])
        self.assertFalse(other["ok"])
        self.assertEqual(other["error"], f"Work not found: {foreign['id']}")
        self.assertFalse(missing["ok"])
        self.assertEqual((await store.get(foreign["id"]))["status"], "running")
        # Clean up the foreign runner task so the loop has nothing pending.
        await runner.cancel(foreign["id"])


if __name__ == "__main__":
    unittest.main()
