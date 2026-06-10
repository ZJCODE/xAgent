import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from xagent.interfaces.cli.processes import pid_is_running, running_pid


class ManagedProcessTests(unittest.TestCase):
    def test_pid_is_running_treats_zombie_process_as_stopped(self):
        with patch("xagent.interfaces.cli.processes.os.kill", return_value=None):
            with patch(
                "xagent.interfaces.cli.processes.subprocess.run",
                return_value=MagicMock(returncode=0, stdout="Z+\n"),
            ):
                self.assertFalse(pid_is_running(123))

    def test_running_pid_removes_zombie_pid_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "weixin.pid"
            pid_path.write_text("123\n", encoding="utf-8")

            with patch("xagent.interfaces.cli.processes.os.kill", return_value=None):
                with patch(
                    "xagent.interfaces.cli.processes.subprocess.run",
                    return_value=MagicMock(returncode=0, stdout="Z\n"),
                ):
                    self.assertIsNone(running_pid(pid_path))

            self.assertFalse(pid_path.exists())