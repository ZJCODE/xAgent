"""Tests for run_command shell tool behavior."""

import unittest

from xagent.core.config import AgentConfig
from xagent.tools.shell_tool import looks_like_long_blocking_command, run_command


class ShellTimeoutMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_stderr_is_actionable(self):
        result = await run_command(command="sleep 2", timeout=1)

        self.assertEqual(result["return_code"], -1)
        self.assertIn("timed out", result["stderr"])
        self.assertIn(f"max {AgentConfig.MAX_COMMAND_TIMEOUT}", result["stderr"])
        self.assertIn("split into smaller commands", result["stderr"])


class LongBlockingHeuristicTests(unittest.TestCase):
    def test_sleep_threshold(self):
        self.assertFalse(looks_like_long_blocking_command("sleep 2", timeout=30))
        self.assertFalse(looks_like_long_blocking_command("sleep 4", timeout=30))
        self.assertTrue(looks_like_long_blocking_command("sleep 5", timeout=30))
        self.assertTrue(looks_like_long_blocking_command("sleep 120", timeout=30))
        self.assertTrue(looks_like_long_blocking_command("sudo sleep 10", timeout=30))
        self.assertTrue(looks_like_long_blocking_command("echo hi; sleep 8", timeout=30))
        self.assertTrue(looks_like_long_blocking_command("sleep 1m", timeout=30))

    def test_poll_loop_with_sleep(self):
        cmd = "for i in $(seq 1 45); do test -f samples.json && break; sleep 2; done"
        self.assertTrue(looks_like_long_blocking_command(cmd, timeout=120))
        self.assertTrue(
            looks_like_long_blocking_command("while true; do sleep 2; done", timeout=30)
        )
        self.assertFalse(looks_like_long_blocking_command("while true; do echo x; done", timeout=10))

    def test_long_timeout_waitish(self):
        self.assertTrue(looks_like_long_blocking_command("python3 monitor.py", timeout=110))
        self.assertTrue(looks_like_long_blocking_command("apt-get update", timeout=120))
        self.assertFalse(looks_like_long_blocking_command("python3 monitor.py", timeout=30))
        self.assertFalse(looks_like_long_blocking_command("echo hello", timeout=120))


if __name__ == "__main__":
    unittest.main()
