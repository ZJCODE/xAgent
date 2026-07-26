"""Tests for the intentionally small shell installer."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = REPO_ROOT / "install.sh"


class InstallScriptTests(unittest.TestCase):
    def test_install_script_syntax(self):
        subprocess.run(["bash", "-n", str(INSTALL_SCRIPT)], check=True)

    def test_installer_uses_uv_and_only_prints_current_commands(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            fake_bin = home / "bin"
            fake_bin.mkdir()
            fake_uv = fake_bin / "uv"
            fake_uv.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env bash",
                        "set -euo pipefail",
                        "printf '%s\\n' \"$*\" >> \"$HOME/uv_calls.log\"",
                        'if [ "${1:-}" = "tool" ] && [ "${2:-}" = "install" ]; then',
                        '  mkdir -p "$HOME/.local/bin"',
                        '  printf \'#!/usr/bin/env sh\\nexit 0\\n\' > "$HOME/.local/bin/xagent"',
                        '  chmod +x "$HOME/.local/bin/xagent"',
                        "fi",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fake_uv.chmod(0o755)
            env = {
                **os.environ,
                "HOME": str(home),
                "PATH": f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
                "XAGENT_NO_PATH_MODIFY": "1",
            }

            result = subprocess.run(
                ["bash", str(INSTALL_SCRIPT)],
                check=True,
                env=env,
                capture_output=True,
                text=True,
            )

            calls = (home / "uv_calls.log").read_text(encoding="utf-8")
            self.assertIn("tool install --force myxagent --python 3.12", calls)
            self.assertIn("xagent setup", result.stdout)
            self.assertIn("xagent web", result.stdout)
            self.assertIn("xagent launcher", result.stdout)
            self.assertIn("xagent start", result.stdout)
            self.assertIn("Use direct commands when scripting", result.stdout)
            self.assertNotIn("processes", result.stdout)
            self.assertNotIn("web start", result.stdout)

    def test_installer_adds_one_path_block(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            fake_bin = home / "bin"
            fake_bin.mkdir()
            fake_uv = fake_bin / "uv"
            fake_uv.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env bash",
                        "set -euo pipefail",
                        'mkdir -p "$HOME/.local/bin"',
                        'printf \'#!/usr/bin/env sh\\nexit 0\\n\' > "$HOME/.local/bin/xagent"',
                        'chmod +x "$HOME/.local/bin/xagent"',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fake_uv.chmod(0o755)
            env = {
                **os.environ,
                "HOME": str(home),
                "PATH": f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
                "SHELL": "/bin/bash",
            }

            subprocess.run(
                ["bash", str(INSTALL_SCRIPT)],
                check=True,
                env=env,
                capture_output=True,
                text=True,
            )

            contents = (home / ".bashrc").read_text(encoding="utf-8")
            self.assertEqual(contents.count("# xAgent PATH"), 1)
            self.assertIn(f'export PATH="{home}/.local/bin:$PATH"', contents)


if __name__ == "__main__":
    unittest.main()
