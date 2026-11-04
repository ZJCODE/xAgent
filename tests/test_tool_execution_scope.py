"""Overlapping tool batches must retain their caller's permission scope."""
import asyncio
import unittest

from xagent.core.tooling.executor import ResourceLocks, ToolExecutor


class ResourceLocksTests(unittest.IsolatedAsyncioTestCase):
    def test_read_only_tools_get_no_lock(self):
        executor = ToolExecutor(None, None, None)
        func = lambda: None
        func.retry_safety = "read_only"
        self.assertIsNone(executor._execution_lock(func))

    def test_undeclared_mutating_tools_share_the_default_write_lock(self):
        executor = ToolExecutor(None, None, None)
        func = lambda: None
        self.assertIs(executor._execution_lock(func), executor.resource_locks.default_write)

    def test_same_scope_shares_a_lock_and_scopes_stay_separate(self):
        executor = ToolExecutor(None, None, None)
        first = lambda: None
        first.write_scope = "workspace"
        second = lambda: None
        second.write_scope = "workspace"
        other = lambda: None
        other.write_scope = "db"
        self.assertIs(executor._execution_lock(first), executor._execution_lock(second))
        self.assertIsNot(executor._execution_lock(first), executor._execution_lock(other))
        self.assertIsNot(executor._execution_lock(other), executor.resource_locks.default_write)

    async def test_scoped_tool_executions_serialize_while_read_only_runs_free(self):
        locks = ResourceLocks()
        active = 0
        peak = 0

        async def hold(name):
            nonlocal active, peak
            async with locks.for_key(name):
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.01)
                active -= 1

        await asyncio.gather(hold("workspace"), hold("workspace"), hold("workspace"))
        self.assertEqual(peak, 1)
        peak = 0
        await asyncio.gather(hold("workspace"), hold("db"))
        self.assertEqual(peak, 2)


class ToolExecutionScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_overlapping_batches_keep_scope_and_restore_after_error(self):
        executor = ToolExecutor(None, None, None)
        first_entered = asyncio.Event()
        second_entered = asyncio.Event()
        observed = []

        async def execute(calls, messages, limit):
            if calls == ['first']:
                first_entered.set()
                await second_entered.wait()
                observed.append(executor._call_scope.get())
                raise RuntimeError('tool failed')
            await first_entered.wait()
            second_entered.set()
            await asyncio.sleep(0)
            observed.append(executor._call_scope.get())

        executor._handle_tool_calls_locked = execute
        results = await asyncio.gather(
            executor.handle_tool_calls(['first'], [], user_id='alice', channel='api', room_name='a'),
            executor.handle_tool_calls(['second'], [], user_id='bob', channel='weixin', room_name='b'),
            return_exceptions=True,
        )
        self.assertIsInstance(results[0], RuntimeError)
        self.assertEqual(set(observed), {('alice', 'api', 'a', ''), ('bob', 'weixin', 'b', '')})
        self.assertEqual(executor._call_scope.get(), ('', None, None, ''))

    async def test_see_image_state_tracks_paths_until_the_next_turn_begins(self):
        executor = ToolExecutor(None, None, None)
        executor.begin_see_image_turn(already_visible=0)
        self.assertIsNone(await executor._queue_see_image_path("first.png"))
        self.assertIsNone(await executor._queue_see_image_path("second.png"))
        self.assertEqual(executor.pending_see_image_paths(), ["first.png", "second.png"])
        # A new turn resets the queue; the caller's context sees nothing.
        executor.begin_see_image_turn(already_visible=4)
        self.assertEqual(executor.pending_see_image_paths(), [])
        # The per-turn cap counts images already visible in the prompt.
        self.assertIsNone(await executor._queue_see_image_path("third.png"))
        self.assertIn("At most", await executor._queue_see_image_path("fourth.png"))
