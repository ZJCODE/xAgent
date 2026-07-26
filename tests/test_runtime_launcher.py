from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from xagent.core.runtime.launcher import (
    RuntimeLaunchError,
    RuntimeLaunchOutcome,
    RuntimeLauncher,
)
from xagent.core.runtime.client import (
    RuntimeClient,
    RuntimeIdentityError,
)
from xagent.interfaces.cli.agents import register_agent
from xagent.interfaces.cli.launcher import run_launcher
from xagent.interfaces.cli.terminal_ui import MenuOption


def _write_agent(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "provider": {
                    "name": "openai",
                    "model": "test-model",
                    "api_key": "test-key",
                },
            }
        ),
        encoding="utf-8",
    )
    (path / "identity.md").write_text("Test Agent\n", encoding="utf-8")


def _status(pid: int, instance_id: str = "instance") -> dict:
    return {
        "running": True,
        "pid": pid,
        "instance_id": instance_id,
        "channels": [],
    }


class _FakeProcess:
    def __init__(self, pid: int = 123, return_code=None) -> None:
        self.pid = pid
        self.return_code = return_code
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.return_code

    def terminate(self):
        self.terminated = True
        self.return_code = -15

    def kill(self):
        self.killed = True
        self.return_code = -9

    def wait(self, timeout=None):
        return self.return_code


class RuntimeLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.agent_dir = Path(self.temporary.name) / "agent"
        _write_agent(self.agent_dir)
        self.launcher = RuntimeLauncher(self.agent_dir)

    def test_invalid_agent_fails_before_spawning(self):
        (self.agent_dir / "identity.md").write_text("", encoding="utf-8")
        with patch("subprocess.Popen") as popen:
            with self.assertRaisesRegex(RuntimeLaunchError, "identity is empty"):
                self.launcher.start()
        popen.assert_not_called()

    def test_start_is_idempotent_when_runtime_is_already_ready(self):
        with patch.object(self.launcher, "status", return_value=_status(91)):
            with patch("subprocess.Popen") as popen:
                outcome = self.launcher.start()
        self.assertEqual(outcome.state, "already_running")
        self.assertEqual(outcome.pid, 91)
        popen.assert_not_called()

    def test_start_uses_private_worker_and_secure_runtime_files(self):
        process = _FakeProcess()
        with patch.object(
            self.launcher,
            "status",
            side_effect=[None, _status(process.pid)],
        ), patch("subprocess.Popen", return_value=process) as popen:
            outcome = self.launcher.start()

        self.assertEqual(outcome.state, "started")
        command = popen.call_args.args[0]
        self.assertEqual(command[1:3], ["-m", "xagent.core.runtime.worker"])
        self.assertNotIn("_runtime", command)
        self.assertEqual(stat.S_IMODE(self.launcher.run_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.launcher.log_path.stat().st_mode), 0o600)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertTrue(popen.call_args.kwargs["close_fds"])

    def test_concurrent_start_accepts_the_runtime_that_won_the_lease(self):
        process = _FakeProcess(pid=123)
        with patch.object(
            self.launcher,
            "status",
            side_effect=[None, _status(456, "winner")],
        ), patch("subprocess.Popen", return_value=process):
            outcome = self.launcher.start()

        self.assertEqual(outcome.state, "already_running")
        self.assertEqual(outcome.pid, 456)
        self.assertTrue(process.terminated)

    def test_child_exit_is_reported_with_log_path(self):
        process = _FakeProcess(return_code=7)
        with patch.object(
            self.launcher,
            "status",
            side_effect=[None, None],
        ), patch("subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(
                RuntimeLaunchError,
                "exited during startup with code 7",
            ):
                self.launcher.start()

    def test_start_timeout_terminates_only_the_spawned_process(self):
        process = _FakeProcess()
        with patch.object(self.launcher, "status", return_value=None), patch(
            "subprocess.Popen",
            return_value=process,
        ), patch(
            "xagent.core.runtime.launcher.time.monotonic",
            side_effect=[0.0, 0.0, 0.0, 1.0],
        ), patch("xagent.core.runtime.launcher.time.sleep"):
            with self.assertRaisesRegex(RuntimeLaunchError, "did not become ready"):
                self.launcher.start(timeout_seconds=0.1)
        self.assertTrue(process.terminated)

    def test_stop_is_idempotent(self):
        with patch.object(self.launcher, "status", return_value=None), patch.object(
            self.launcher.client,
            "request",
        ) as request:
            outcome = self.launcher.stop()
        self.assertEqual(outcome.state, "already_stopped")
        request.assert_not_called()

    def test_stop_waits_for_the_same_instance_to_disappear(self):
        running = _status(123, "one")
        with patch.object(
            self.launcher,
            "status",
            side_effect=[running, None],
        ), patch.object(self.launcher.client, "request") as request:
            outcome = self.launcher.stop()
        self.assertEqual(outcome.state, "stopped")
        request.assert_called_once_with(
            "POST",
            "/v1/runtime/stop",
            timeout=5.0,
        )

    def test_stop_never_claims_a_replacement_instance_was_stopped(self):
        with patch.object(
            self.launcher,
            "status",
            side_effect=[_status(123, "one"), _status(456, "two")],
        ), patch.object(self.launcher.client, "request"):
            with self.assertRaisesRegex(RuntimeLaunchError, "replaced"):
                self.launcher.stop()

    def test_restart_composes_stop_and_start(self):
        started = RuntimeLaunchOutcome(
            state="started",
            pid=234,
            instance_id="new",
            log_path=self.launcher.log_path,
        )
        with patch.object(self.launcher, "stop") as stop, patch.object(
            self.launcher,
            "start",
            return_value=started,
        ) as start:
            outcome = self.launcher.restart()
        stop.assert_called_once_with(timeout_seconds=15.0)
        start.assert_called_once_with(timeout_seconds=10.0)
        self.assertEqual(outcome.state, "restarted")
        self.assertEqual(outcome.pid, 234)

    def test_real_worker_reaches_ready_and_stops_through_control_plane(self):
        outcome = self.launcher.start(timeout_seconds=10.0)
        try:
            self.assertEqual(outcome.state, "started")
            status = self.launcher.status()
            self.assertIsNotNone(status)
            self.assertEqual(status["pid"], outcome.pid)
            self.assertEqual(
                stat.S_IMODE(self.launcher.client.info_path.stat().st_mode),
                0o600,
            )
        finally:
            stopped = self.launcher.stop(timeout_seconds=15.0)
        self.assertEqual(stopped.state, "stopped")
        self.assertIsNone(self.launcher.status())

    @unittest.skipIf(os.name == "nt", "POSIX symbolic-link check")
    def test_runtime_directory_must_not_be_a_symbolic_link(self):
        target = Path(self.temporary.name) / "external-run"
        target.mkdir()
        (self.agent_dir / "run").symlink_to(target, target_is_directory=True)
        with patch.object(self.launcher, "status", return_value=None), patch(
            "subprocess.Popen",
        ) as popen:
            with self.assertRaisesRegex(RuntimeLaunchError, "symbolic link"):
                self.launcher.start()
        popen.assert_not_called()


class RuntimeClientSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        run_dir = self.root / "run"
        run_dir.mkdir()
        self.info_path = run_dir / "runtime.json"
        self.info = {
            "pid": 123,
            "instance_id": "instance",
            "control_url": "http://127.0.0.1:43210",
            "token": "s" * 43,
            "started_at": 1.0,
        }
        self.info_path.write_text(json.dumps(self.info), encoding="utf-8")
        self.info_path.chmod(0o600)
        self.client = RuntimeClient(self.root)

    def test_control_requests_never_use_environment_proxies(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "running": True,
            "pid": 123,
            "instance_id": "instance",
            "channels": [],
        }
        with patch(
            "xagent.core.runtime.client.httpx.request",
            return_value=response,
        ) as request:
            status = self.client.status()
        self.assertTrue(status["running"])
        self.assertFalse(request.call_args.kwargs["trust_env"])

    def test_status_requires_control_plane_identity_to_match_runtime_file(self):
        with patch.object(
            self.client,
            "_request",
            return_value={
                "running": True,
                "pid": 999,
                "instance_id": "different",
                "channels": [],
            },
        ):
            with self.assertRaisesRegex(RuntimeIdentityError, "identity mismatch"):
                self.client.status()

    @unittest.skipIf(os.name == "nt", "POSIX permission check")
    def test_runtime_token_file_rejects_group_or_world_access(self):
        self.info_path.chmod(0o644)
        with self.assertRaisesRegex(RuntimeIdentityError, "permissions must be 0600"):
            self.client.info()

    @unittest.skipIf(os.name == "nt", "POSIX symbolic-link check")
    def test_runtime_token_file_must_not_be_a_symbolic_link(self):
        target = self.root / "token-target.json"
        target.write_text(json.dumps(self.info), encoding="utf-8")
        target.chmod(0o600)
        self.info_path.unlink()
        self.info_path.symlink_to(target)
        with self.assertRaisesRegex(RuntimeIdentityError, "cannot open runtime.json"):
            self.client.info()


class _ScriptedUI:
    def __init__(self, keys: list[str]) -> None:
        self.keys = iter(keys)
        self.menus: list[tuple[str, tuple[MenuOption, ...]]] = []
        self.panels: list[str] = []

    def select_menu(self, *, title, options, **kwargs):
        rows = tuple(options)
        self.menus.append((title, rows))
        key = next(self.keys)
        return next(row for row in rows if row.key == key)

    def clear(self):
        return None

    def pause(self, *args, **kwargs):
        return None

    def print_panel(self, message, **kwargs):
        self.panels.append(str(message))


class InteractiveLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.agent_dir = self.root / "agents" / "alpha"
        _write_agent(self.agent_dir)
        register_agent(
            "alpha",
            path=self.agent_dir,
            make_active=True,
            root=self.root,
        )

    def test_runtime_menu_delegates_to_the_public_cli(self):
        ui = _ScriptedUI(["runtime", "start", "back", "exit"])
        commands: list[list[str]] = []

        with patch.object(RuntimeLauncher, "status", return_value=None):
            result = run_launcher(
                dispatch=lambda command: commands.append(list(command)) or 0,
                ui=ui,
                registry_root=self.root,
            )

        self.assertEqual(result, 0)
        self.assertEqual(commands, [["start", "--agent", "alpha"]])
        self.assertNotIn(
            "web",
            {option.key for _, options in ui.menus for option in options},
        )

    def test_channel_menu_delegates_without_constructing_an_agent(self):
        ui = _ScriptedUI(["channels", "api", "restart", "back", "exit"])
        commands: list[list[str]] = []

        with patch.object(RuntimeLauncher, "status", return_value=None):
            run_launcher(
                dispatch=lambda command: commands.append(list(command)) or 0,
                ui=ui,
                registry_root=self.root,
            )

        self.assertEqual(
            commands,
            [["channel", "restart", "api", "--agent", "alpha"]],
        )
