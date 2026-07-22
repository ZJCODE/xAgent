"""Tests for installation-aware xAgent self-updates."""

from __future__ import annotations

import argparse
import io
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from xagent.interfaces.cli import build_parser
from xagent.interfaces.cli import update as updater


def _snapshot(
    root: Path,
    *,
    installer: str = "pip",
    metadata_name: str = "myxagent-1.2.3.dist-info",
    direct_url: dict | None = None,
) -> updater.DistributionSnapshot:
    return updater.DistributionSnapshot(
        version="1.2.3",
        installer=installer,
        package_root=root,
        metadata_path=root / metadata_name,
        direct_url=direct_url,
    )


def _completed(command: list[str], returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


class UpdateDetectionTests(unittest.TestCase):
    def test_source_install_is_rejected_before_package_manager_detection(self):
        root = Path("/workspace/xagent")
        snapshot = _snapshot(root, installer="", metadata_name="myxagent.egg-info")

        with patch.object(updater, "_read_distribution_snapshot", return_value=snapshot):
            with patch.object(updater, "_detect_uv_tool") as detect_uv:
                with self.assertRaises(updater.InstallationDetectionError) as raised:
                    updater.detect_installation()

        self.assertIn("development or source", str(raised.exception))
        detect_uv.assert_not_called()

    def test_direct_url_source_install_is_rejected(self):
        root = Path("/workspace/site-packages")
        snapshot = _snapshot(
            root,
            direct_url={"url": "file:///workspace/xagent", "dir_info": {"editable": True}},
        )

        with patch.object(updater, "_read_distribution_snapshot", return_value=snapshot):
            with self.assertRaises(updater.InstallationDetectionError) as raised:
                updater.detect_installation()

        self.assertIn("development or source", str(raised.exception))

    def test_uv_tool_requires_current_prefix_and_registration(self):
        tool_dir = Path("/tmp/uv-tools")
        environment = tool_dir / "myxagent"
        snapshot = _snapshot(environment / "lib/python3.12/site-packages", installer="uv")
        uv = Path("/tmp/bin/uv")

        def run_capture(command):
            if tuple(command[1:]) == ("tool", "dir"):
                return _completed(list(command), stdout=f"{tool_dir}\n")
            if tuple(command[1:]) == ("tool", "list"):
                return _completed(
                    list(command),
                    stdout=f"myxagent v1.2.3 ({environment})\n- xagent (/tmp/bin/xagent)\n",
                )
            self.fail(f"unexpected command: {command}")

        with patch.object(updater, "_read_distribution_snapshot", return_value=snapshot):
            with patch.object(updater, "_find_uv", return_value=uv):
                with patch.object(updater, "_run_capture", side_effect=run_capture):
                    with patch.object(updater.sys, "prefix", str(environment)):
                        with patch.object(updater.sys, "executable", str(environment / "bin/python")):
                            installation = updater.detect_installation()

        self.assertEqual(installation.kind, updater.INSTALLATION_UV_TOOL)
        self.assertEqual(installation.update_command, (str(uv), "tool", "upgrade", "myxagent"))

    def test_uv_installed_elsewhere_does_not_override_current_pip_environment(self):
        prefix = Path("/tmp/current-python")
        package_root = prefix / "lib/python3.12/site-packages"
        snapshot = _snapshot(package_root, installer="pip")

        def run_capture(command):
            if tuple(command[1:]) == ("tool", "dir"):
                return _completed(list(command), stdout="/tmp/other-uv-tools\n")
            if tuple(command[1:]) == ("-m", "pip", "--version"):
                return _completed(list(command), stdout="pip 26.0\n")
            self.fail(f"unexpected command: {command}")

        with patch.object(updater, "_read_distribution_snapshot", return_value=snapshot):
            with patch.object(updater, "_find_uv", return_value=Path("/tmp/bin/uv")):
                with patch.object(updater, "_run_capture", side_effect=run_capture):
                    with patch.object(updater.sys, "prefix", str(prefix)):
                        with patch.object(updater.sys, "executable", str(prefix / "bin/python")):
                            with patch.object(updater.site, "getusersitepackages", return_value="/tmp/user-site"):
                                installation = updater.detect_installation()

        self.assertEqual(installation.kind, updater.INSTALLATION_PIP)
        self.assertEqual(
            installation.update_command,
            (str(prefix / "bin/python"), "-m", "pip", "install", "--upgrade", "myxagent"),
        )

    def test_pip_user_install_preserves_user_scope(self):
        prefix = Path("/opt/python")
        user_site = Path("/tmp/user-site")
        snapshot = _snapshot(user_site, installer="pip")

        with patch.object(updater, "_find_uv", return_value=None):
            with patch.object(
                updater,
                "_run_capture",
                return_value=_completed([], stdout="pip 26.0\n"),
            ):
                with patch.object(updater.site, "getusersitepackages", return_value=str(user_site)):
                    with patch.object(updater.sys, "prefix", str(prefix)):
                        with patch.object(updater.sys, "executable", str(prefix / "bin/python")):
                            installation = updater._detect_pip(snapshot)

        self.assertIsNotNone(installation)
        assert installation is not None
        self.assertIn("--user", installation.update_command)

    def test_uv_metadata_without_uv_executable_is_rejected(self):
        snapshot = _snapshot(Path("/tmp/site-packages"), installer="uv")

        with patch.object(updater, "_read_distribution_snapshot", return_value=snapshot):
            with patch.object(updater, "_find_uv", return_value=None):
                with self.assertRaises(updater.InstallationDetectionError) as raised:
                    updater.detect_installation()

        self.assertIn("could not be found", str(raised.exception))

    def test_unknown_installer_is_rejected(self):
        snapshot = _snapshot(Path("/tmp/site-packages"), installer="conda")

        with patch.object(updater, "_read_distribution_snapshot", return_value=snapshot):
            with patch.object(updater, "_find_uv", return_value=None):
                with self.assertRaises(updater.InstallationDetectionError) as raised:
                    updater.detect_installation()

        self.assertIn("Unable to safely determine", str(raised.exception))


class UpdateCommandTests(unittest.TestCase):
    def _installation(self) -> updater.InstallationInfo:
        return updater.InstallationInfo(
            kind=updater.INSTALLATION_PIP,
            version="1.2.3",
            package_root=Path("/tmp/python/site-packages"),
            python=Path("/tmp/python/bin/python"),
            update_command=("/tmp/python/bin/python", "-m", "pip", "install", "--upgrade", "myxagent"),
        )

    def test_parser_registers_update_restart_modes(self):
        default = build_parser().parse_args(["update"])
        restart = build_parser().parse_args(["update", "--restart"])
        no_restart = build_parser().parse_args(["update", "--no-restart"])

        self.assertIsNone(default.restart)
        self.assertTrue(restart.restart)
        self.assertFalse(no_restart.restart)
        self.assertIs(default.handler, updater.handle_update)
        self.assertIn("  update", build_parser().format_help())

    def test_public_command_reenters_hidden_worker_with_restart_mode(self):
        args = argparse.Namespace(restart=False)
        with patch.object(updater.os, "execv") as execv:
            exit_code = updater.handle_update(args)

        self.assertEqual(exit_code, 1)
        execv.assert_called_once_with(
            updater.sys.executable,
            [
                updater.sys.executable,
                "-m",
                "xagent.interfaces.cli",
                "_update-worker",
                "--no-restart",
            ],
        )

    def test_worker_reports_already_current_without_restarting(self):
        installation = self._installation()
        with patch.object(updater, "detect_installation", return_value=installation):
            with patch.object(updater.subprocess, "run", return_value=_completed([], returncode=0)) as run:
                with patch.object(updater, "_fresh_installed_version", return_value="1.2.3"):
                    with patch.object(updater, "_restart_running_processes") as restart:
                        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                            exit_code = updater.handle_update_worker(argparse.Namespace(restart=None))

        self.assertEqual(exit_code, 0)
        run.assert_called_once_with(list(installation.update_command), check=False)
        restart.assert_not_called()
        self.assertIn("already up to date", stdout.getvalue())

    def test_worker_verifies_new_version_then_restarts(self):
        installation = self._installation()
        with patch.object(updater, "detect_installation", return_value=installation):
            with patch.object(updater.subprocess, "run", return_value=_completed([], returncode=0)):
                with patch.object(updater, "_fresh_installed_version", return_value="1.3.0"):
                    with patch.object(updater, "_restart_running_processes", return_value=0) as restart:
                        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                            exit_code = updater.handle_update_worker(argparse.Namespace(restart=True))

        self.assertEqual(exit_code, 0)
        restart.assert_called_once_with(True)
        self.assertIn("1.2.3 -> 1.3.0", stdout.getvalue())

    def test_worker_returns_failure_and_recovery_command(self):
        installation = self._installation()
        with patch.object(updater, "detect_installation", return_value=installation):
            with patch.object(updater.subprocess, "run", return_value=_completed([], returncode=7)):
                with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    exit_code = updater.handle_update_worker(argparse.Namespace(restart=None))

        self.assertEqual(exit_code, 1)
        self.assertIn("update failed with exit code 7", stdout.getvalue())
        self.assertIn("Retry manually with", stdout.getvalue())

    def test_noninteractive_restart_mode_only_prints_reminder(self):
        with patch.object(updater, "iter_running_process_refs", return_value=iter([object()])):
            with patch.object(updater, "handle_processes_restart") as restart:
                with patch("sys.stdin", io.StringIO()):
                    with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                        exit_code = updater._restart_running_processes(None)

        self.assertEqual(exit_code, 0)
        restart.assert_not_called()
        self.assertIn("xagent processes restart", stdout.getvalue())

    def test_forced_restart_propagates_restart_failure(self):
        with patch.object(updater, "iter_running_process_refs", return_value=iter([object(), object()])):
            with patch.object(updater, "handle_processes_restart", return_value=1) as restart:
                with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    exit_code = updater._restart_running_processes(True)

        self.assertEqual(exit_code, 1)
        restart.assert_called_once()
        self.assertIn("failed to restart", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
