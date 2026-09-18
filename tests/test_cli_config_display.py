import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from xagent.interfaces.cli.config_display import format_config_for_display, redact_secret_value
from xagent.interfaces.cli.cli_hints import misplaced_agent_flag_hint
from argparse import Namespace

from xagent.interfaces.cli.runtime import handle_config


class CliConfigDisplayTests(unittest.TestCase):
    def test_redact_secret_value(self):
        self.assertEqual(redact_secret_value("sk-1234567890abcdef"), "sk-1…cdef")

    def test_format_config_redacts_api_key(self):
        raw = yaml.safe_dump(
            {"provider": {"api_key": "sk-1234567890abcdef", "model": "gpt-4"}},
            sort_keys=False,
        )
        text, redacted = format_config_for_display(raw)
        self.assertTrue(redacted)
        self.assertNotIn("1234567890abcdef", text)
        self.assertIn("gpt-4", text)

    def test_misplaced_agent_hint_for_chat(self):
        hint = misplaced_agent_flag_hint(["--agent", "mira", "chat", "hi"])
        self.assertIsNotNone(hint)
        self.assertIn("xagent chat --agent mira", hint or "")

    def test_config_show_redacts_on_disk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            (config_dir / "config.yaml").write_text(
                yaml.safe_dump({"provider": {"api_key": "sk-secretvalue123456"}}),
                encoding="utf-8",
            )
            args = Namespace(
                config_command="show",
                secrets=False,
                agent=None,
                config_dir=str(config_dir),
            )
            with patch("xagent.interfaces.cli.runtime.runtime_dir", return_value=config_dir):
                with patch("sys.stderr", new_callable=io.StringIO):
                    buf = io.StringIO()
                    with patch("sys.stdout", buf):
                        code = handle_config(args)
            self.assertEqual(code, 0)
            self.assertNotIn("secretvalue123456", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
