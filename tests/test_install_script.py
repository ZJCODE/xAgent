"""Tests for the shell installer."""

from __future__ import annotations

import json
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

    def test_installer_prefers_uv_tool_over_python_pip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            fake_bin = home / "bin"
            fake_bin.mkdir()

            uv_log = home / "uv_calls.log"
            fake_uv = fake_bin / "uv"
            fake_uv.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env bash",
                        "set -euo pipefail",
                        "printf '%s\\n' \"$*\" >> \"$HOME/uv_calls.log\"",
                        "if [ \"${1:-}\" = \"tool\" ] && [ \"${2:-}\" = \"install\" ]; then",
                        "  mkdir -p \"$HOME/.local/bin\"",
                        "  printf '#!/usr/bin/env sh\\nexit 0\\n' > \"$HOME/.local/bin/xagent\"",
                        "  chmod +x \"$HOME/.local/bin/xagent\"",
                        "fi",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fake_uv.chmod(0o755)

            fake_python = fake_bin / "python3"
            fake_python.write_text(
                "#!/usr/bin/env sh\nprintf 'python should not be called\\n' >> \"$HOME/python_calls.log\"\nexit 1\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)

            env = {
                **os.environ,
                "HOME": str(home),
                "PATH": f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
                "XAGENT_NO_PATH_MODIFY": "1",
            }
            subprocess.run(["bash", str(INSTALL_SCRIPT)], check=True, env=env, capture_output=True, text=True)

            self.assertFalse((home / "python_calls.log").exists())
            self.assertIn("tool install --force myxagent --python 3.12", uv_log.read_text(encoding="utf-8"))

            manifest = json.loads((home / ".xagent" / "cli.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["command"], [str(home / ".local" / "bin" / "xagent")])

    def test_installer_adds_bindir_to_shell_profile(self):
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
                        "if [ \"${1:-}\" = \"tool\" ] && [ \"${2:-}\" = \"install\" ]; then",
                        "  mkdir -p \"$HOME/.local/bin\"",
                        "  printf '#!/usr/bin/env sh\\nexit 0\\n' > \"$HOME/.local/bin/xagent\"",
                        "  chmod +x \"$HOME/.local/bin/xagent\"",
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
                "SHELL": "/bin/bash",
            }
            subprocess.run(["bash", str(INSTALL_SCRIPT)], check=True, env=env, capture_output=True, text=True)

            bashrc = home / ".bashrc"
            self.assertTrue(bashrc.is_file())
            contents = bashrc.read_text(encoding="utf-8")
            self.assertIn("# xAgent PATH", contents)
            self.assertIn(f'export PATH="{home}/.local/bin:$PATH"', contents)


if __name__ == "__main__":
    unittest.main()
