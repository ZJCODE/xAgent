"""Tests for run_command shell tool behavior."""

import os
import unittest
from unittest.mock import patch

from xagent.core.config import AgentConfig
from xagent.tools.shell_tool import run_command


class ShellTimeoutMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_stderr_is_actionable(self):
        result = await run_command(command="sleep 2", timeout=1)

        self.assertEqual(result["return_code"], -1)
        self.assertIn("timed out", result["stderr"])
        self.assertIn(f"max {AgentConfig.MAX_COMMAND_TIMEOUT}", result["stderr"])
        self.assertIn("split into smaller commands", result["stderr"])

    async def test_subprocess_env_strips_host_provider_and_channel_secrets(self):
        fake_secrets = {
            "OPENAI_API_KEY": "sk-secret-openai",
            "ANTHROPIC_API_KEY": "sk-ant-secret",
            "SONIOX_API_KEY": "soniox-secret",
            "LANGFUSE_SECRET_KEY": "lf-secret",
            "WEIXIN_BOT_TOKEN": "wx-token",
            "NORMAL_TOOL_VAR": "preserved-value",
        }
        with patch.dict(os.environ, fake_secrets, clear=False):
            result = await run_command(
                command="python3 -c \"import os, json; print(json.dumps({k: os.environ.get(k) for k in ['OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'SONIOX_API_KEY', 'LANGFUSE_SECRET_KEY', 'WEIXIN_BOT_TOKEN', 'NORMAL_TOOL_VAR']}))\""
            )
        self.assertEqual(result["return_code"], 0)
        import json
        child_env = json.loads(result["stdout"])
        self.assertIsNone(child_env["OPENAI_API_KEY"])
        self.assertIsNone(child_env["ANTHROPIC_API_KEY"])
        self.assertIsNone(child_env["SONIOX_API_KEY"])
        self.assertIsNone(child_env["LANGFUSE_SECRET_KEY"])
        self.assertIsNone(child_env["WEIXIN_BOT_TOKEN"])
        self.assertEqual(child_env["NORMAL_TOOL_VAR"], "preserved-value")


if __name__ == "__main__":
    unittest.main()
