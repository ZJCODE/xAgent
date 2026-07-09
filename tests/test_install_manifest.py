"""Tests for the CLI install manifest (~/.xagent/cli.json)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xagent.interfaces.cli import install_manifest


class InstallManifestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def test_manifest_path_is_under_config_dir(self):
        self.assertEqual(install_manifest.manifest_path(), self.home / ".xagent" / "cli.json")

    def test_record_writes_valid_json(self):
        binary = self.home / "bin" / "xagent"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n")

        with mock.patch("shutil.which", return_value=str(binary)):
            install_manifest.record_cli_location()

        data = json.loads(install_manifest.manifest_path().read_text())
        self.assertEqual(data["command"], [str(binary.resolve())])
        self.assertEqual(data["binary"], str(binary.resolve()))
        self.assertIn("updated_at", data)

    def test_record_from_python_module_writes_module_command(self):
        module_entry = self.home / "src" / "xagent" / "__main__.py"
        module_entry.parent.mkdir(parents=True)
        module_entry.write_text("# module entry\n")
        python = str(self.home / "Python Bin" / "python3")

        with mock.patch("shutil.which", return_value=None), mock.patch.object(
            install_manifest.sys, "argv", [str(module_entry)]
        ), mock.patch.object(install_manifest.sys, "executable", python):
            install_manifest.record_cli_location()

        data = json.loads(install_manifest.manifest_path().read_text())
        self.assertEqual(data["command"], [python, "-m", "xagent"])
        self.assertEqual(data["binary"], "")

    def test_record_and_read_roundtrip(self):
        binary = self.home / "bin" / "xagent"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n")

        with mock.patch("shutil.which", return_value=str(binary)):
            install_manifest.record_cli_location()

        self.assertEqual(install_manifest.read_cli_binary(), binary.resolve())
        self.assertEqual(install_manifest.read_cli_command(), [str(binary.resolve())])

    def test_record_skips_when_binary_unresolvable(self):
        with mock.patch("shutil.which", return_value=None), mock.patch.object(
            install_manifest.sys, "argv", ["python -m xagent"]
        ):
            install_manifest.record_cli_location()
        self.assertFalse(install_manifest.manifest_path().exists())

    def test_read_returns_none_for_missing_manifest(self):
        self.assertIsNone(install_manifest.read_cli_binary())
        self.assertIsNone(install_manifest.read_cli_command())

    def test_read_returns_none_for_corrupt_manifest(self):
        manifest = install_manifest.manifest_path()
        manifest.parent.mkdir(parents=True)
        manifest.write_text("not json")
        self.assertIsNone(install_manifest.read_cli_binary())
        self.assertIsNone(install_manifest.read_cli_command())

    def test_read_returns_none_when_binary_missing_on_disk(self):
        manifest = install_manifest.manifest_path()
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"command": ["/usr/bin/python3", "-m", "xagent"], "binary": str(self.home / "gone" / "xagent")}))
        self.assertIsNone(install_manifest.read_cli_binary())

    def test_read_command_rejects_invalid_command(self):
        manifest = install_manifest.manifest_path()
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"command": ["/usr/bin/python3", ""]}))
        self.assertIsNone(install_manifest.read_cli_command())


if __name__ == "__main__":
    unittest.main()
