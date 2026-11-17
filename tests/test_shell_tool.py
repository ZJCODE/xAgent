"""Tests for run_command shell tool behavior."""

import unittest
import asyncio
import os
import sys
import shlex
import time

from xagent.core.config import AgentConfig
from xagent.tools.shell_tool import run_command


class ShellTimeoutMessageTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX process groups")
    async def test_timeout_kills_descendants_holding_pipes(self):
        started = time.monotonic()
        result = await asyncio.wait_for(run_command(command="sleep 10 & wait", timeout=1), 4)
        self.assertEqual(result["return_code"], -1)
        self.assertLess(time.monotonic() - started, 4)

    @unittest.skipUnless(os.name == "posix", "POSIX process groups")
    async def test_cancellation_cleans_process_group(self):
        task = asyncio.create_task(run_command(command="sleep 10 & wait", timeout=30))
        await asyncio.sleep(0.1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)

    @unittest.skipUnless(os.name == "posix", "POSIX process groups")
    async def test_force_kills_term_resistant_descendant(self):
        program = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(10)"
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(program)} & wait"
        result = await asyncio.wait_for(run_command(command=command, timeout=1), 4)
        self.assertEqual(result["return_code"], -1)

    async def test_timeout_stderr_is_actionable(self):
        result = await run_command(command="sleep 2", timeout=1)

        self.assertEqual(result["return_code"], -1)
        self.assertIn("timed out", result["stderr"])
        self.assertIn(f"max {AgentConfig.MAX_COMMAND_TIMEOUT}", result["stderr"])
        self.assertIn("split into smaller commands", result["stderr"])


if __name__ == "__main__":
    unittest.main()
