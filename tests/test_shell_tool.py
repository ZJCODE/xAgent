"""Tests for run_command shell tool behavior."""

import unittest

from xagent.core.config import AgentConfig
from xagent.tools.shell_tool import run_command


class ShellTimeoutMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_stderr_is_actionable(self):
        result = await run_command(command="sleep 2", timeout=1)

        self.assertEqual(result["return_code"], -1)
        self.assertIn("timed out", result["stderr"])
        self.assertIn(f"max {AgentConfig.MAX_COMMAND_TIMEOUT}", result["stderr"])
        self.assertIn("split into smaller commands", result["stderr"])


if __name__ == "__main__":
    unittest.main()
