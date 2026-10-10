import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from xagent.interfaces.cli.agents import register_agent
from xagent.interfaces.cli.processes import (
    iter_managed_process_refs,
    pid_is_running,
    process_status_row,
    running_pid,
)
from xagent.interfaces.cli.processes_status import handle_processes_restart, handle_processes_status


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

    def test_iter_managed_process_refs_includes_web_and_registered_agents(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            with patch("xagent.interfaces.cli.agents.BaseAgentConfig.DEFAULT_CONFIG_DIR", str(root)):
                register_agent("default", title="Default", make_active=True)
                register_agent("work", title="Work")

                refs = iter_managed_process_refs(root=root)
                labels = {
                    (ref.scope, ref.agent, ref.channel)
                    for ref in refs
                }

                self.assertIn(("web", None, None), labels)
                self.assertEqual(labels, {("web", None, None),
                                          ("agent", "default", "runtime"),
                                          ("agent", "work", "runtime")})
                for ref in refs:
                    if ref.scope == "agent":
                        self.assertEqual(ref.pid_path, ref.config_dir / "run" / "runtime.pid")
                        self.assertEqual(ref.log_path, ref.config_dir / "logs" / "runtime.log")

    def test_iter_managed_process_refs_falls_back_to_agents_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            agent_dir = root / "agents" / "legacy"
            pid_path = agent_dir / "run" / "runtime.pid"
            pid_path.parent.mkdir(parents=True)
            pid_path.write_text("42\n", encoding="utf-8")

            refs = iter_managed_process_refs(root=root)
            runtime_refs = [ref for ref in refs if ref.agent == "legacy" and ref.channel == "runtime"]

            self.assertEqual(len(runtime_refs), 1)
            self.assertEqual(runtime_refs[0].config_dir, agent_dir.resolve())
            self.assertEqual(runtime_refs[0].pid_path, pid_path)

    def test_processes_status_json_reports_running_count_and_exit_code(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            web_pid = root / "run" / "web.pid"
            web_pid.parent.mkdir(parents=True)
            web_pid.write_text("99\n", encoding="utf-8")

            args = unittest.mock.Mock(json_output=True)
            with patch("xagent.interfaces.cli.processes_status.iter_managed_process_refs") as refs:
                from xagent.interfaces.cli.processes import ManagedProcessRef

                refs.return_value = [
                    ManagedProcessRef(scope="web", pid_path=web_pid, log_path=root / "logs" / "web.log"),
                ]
                with patch("xagent.interfaces.cli.processes.running_pid", return_value=99):
                    with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                        exit_code = handle_processes_status(args)

            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["running_count"], 1)
            self.assertEqual(exit_code, 2)

    def test_processes_restart_only_restarts_running_processes(self):
        import argparse

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            web_pid = root / "run" / "web.pid"
            web_pid.parent.mkdir(parents=True)
            web_pid.write_text("99\n", encoding="utf-8")
            from xagent.interfaces.cli.processes import ManagedProcessRef

            running_ref = ManagedProcessRef(
                scope="web",
                pid_path=web_pid,
                log_path=root / "logs" / "web.log",
            )
            args = argparse.Namespace(json_output=False)
            with patch("xagent.interfaces.cli.processes_status.iter_running_process_refs", return_value=[running_ref]):
                with patch("xagent.interfaces.cli.processes_status.stop_managed_process", return_value=(True, "stopped")):
                    with patch(
                        "xagent.interfaces.cli.processes_status._start_background_web",
                        return_value=(True, False),
                    ) as web_restart:
                        with patch("xagent.interfaces.cli.processes_status.start_runtime") as runtime_restart:
                            with patch("sys.stdout", new_callable=io.StringIO):
                                exit_code = handle_processes_restart(args)

            self.assertEqual(exit_code, 0)
            web_restart.assert_called_once()
            runtime_restart.assert_not_called()

    def test_processes_restart_restarts_one_runtime_per_agent(self):
        import argparse
        from xagent.interfaces.cli.processes import ManagedProcessRef
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir).resolve() / "agents" / "work"
            ref = ManagedProcessRef(scope="agent", agent="work", channel="runtime",
                                    config_dir=config_dir, pid_path=config_dir / "run" / "runtime.pid",
                                    log_path=config_dir / "logs" / "runtime.log")
            calls = []
            def stop(path):
                calls.append(("stop", path))
                return True, "stopped"
            def start(root):
                calls.append(("start", root))
                return {"status": "running"}
            with patch("xagent.interfaces.cli.processes_status.iter_running_process_refs", return_value=[ref]), patch(
                "xagent.interfaces.cli.processes_status.stop_managed_process", side_effect=stop,
            ), patch("xagent.interfaces.cli.processes_status.start_runtime", side_effect=start), patch(
                "xagent.interfaces.cli.processes_status._start_background_web",
            ) as web_restart, patch("sys.stdout", new_callable=io.StringIO) as stdout:
                exit_code = handle_processes_restart(argparse.Namespace(json_output=True))
            self.assertEqual(exit_code, 0)
            self.assertEqual(calls, [("stop", ref.pid_path), ("start", config_dir)])
            self.assertEqual(json.loads(stdout.getvalue())["restarted"][0]["label"], "work/runtime")
            web_restart.assert_not_called()

    def test_processes_restart_does_not_start_runtime_if_shutdown_fails(self):
        import argparse
        from xagent.interfaces.cli.processes import ManagedProcessRef
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ref = ManagedProcessRef(scope="agent", agent="work", channel="runtime", config_dir=root,
                                    pid_path=root / "runtime.pid", log_path=root / "runtime.log")
            with patch("xagent.interfaces.cli.processes_status.iter_running_process_refs", return_value=[ref]), patch(
                "xagent.interfaces.cli.processes_status.stop_managed_process", return_value=(False, "still draining"),
            ), patch("xagent.interfaces.cli.processes_status.start_runtime") as start, patch(
                "sys.stdout", new_callable=io.StringIO,
            ) as stdout:
                exit_code = handle_processes_restart(argparse.Namespace(json_output=True))
            self.assertEqual(exit_code, 1)
            self.assertEqual(json.loads(stdout.getvalue())["restarted"][0]["message"], "still draining")
            start.assert_not_called()

    def test_process_status_row_marks_running_and_stopped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "api.pid"
            pid_path.write_text("55\n", encoding="utf-8")
            from xagent.interfaces.cli.processes import ManagedProcessRef

            ref = ManagedProcessRef(
                scope="agent",
                agent="default",
                channel="api",
                config_dir=Path(tmpdir),
                pid_path=pid_path,
                log_path=Path(tmpdir) / "api.log",
            )

            with patch("xagent.interfaces.cli.processes.running_pid", return_value=55):
                running_row = process_status_row(ref)
            with patch("xagent.interfaces.cli.processes.running_pid", return_value=None):
                stopped_row = process_status_row(ref)

            self.assertEqual(running_row["status"], "running")
            self.assertEqual(running_row["pid"], 55)
            self.assertEqual(stopped_row["status"], "stopped")
            self.assertIsNone(stopped_row["pid"])
