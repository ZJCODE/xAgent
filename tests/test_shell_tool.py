"""Tests for run_command shell tool behavior."""

import asyncio
import time
import unittest

from xagent.core.config import AgentConfig
from xagent.core.turn import TurnCancel, bind_turn_cancel, reset_turn_cancel
from xagent.tools.shell_tool import run_command


class ShellTimeoutMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_stderr_is_actionable(self):
        result = await run_command(command="sleep 2", timeout=1)

        self.assertEqual(result["return_code"], -1)
        self.assertIn("timed out", result["stderr"])
        self.assertIn(f"max {AgentConfig.MAX_COMMAND_TIMEOUT}", result["stderr"])
        self.assertIn("split into smaller commands", result["stderr"])

    async def test_run_command_aborts_when_turn_is_cancelled(self):
        cancel = TurnCancel()
        token = bind_turn_cancel(cancel)
        try:
            task = asyncio.create_task(run_command(command="sleep 30", timeout=30))
            await asyncio.sleep(0.3)
            cancel.request()
            started = time.monotonic()
            result = await asyncio.wait_for(task, timeout=5)
            elapsed = time.monotonic() - started
        finally:
            reset_turn_cancel(token)

        self.assertEqual(result["return_code"], -1)
        self.assertEqual(result["stderr"], "Command aborted.")
        self.assertLess(elapsed, 2)


if __name__ == "__main__":
    unittest.main()
