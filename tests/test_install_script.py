"""Tests for the shell installer."""

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
            self.assertFalse((home / ".xagent" / "cli.json").exists())

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

    def test_installer_shows_getting_started_commands(self):
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
                ["bash", str(INSTALL_SCRIPT)], check=True, env=env, capture_output=True, text=True
            )

            self.assertIn("Get started:", result.stdout)
            self.assertIn("xagent web start --open", result.stdout)
            self.assertIn("xagent --help", result.stdout)
            self.assertNotIn("To run xagent in this terminal:", result.stdout)

    def _write_fake_upgrade_tools(self, home: Path) -> tuple[Path, Path]:
        fake_bin = home / "bin"
        fake_bin.mkdir()

        fake_uv = fake_bin / "uv"
        fake_uv.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    'if [ "${1:-}" = "tool" ] && [ "${2:-}" = "install" ]; then',
                    '  mkdir -p "$HOME/.local/bin"',
                    "  cat > \"$HOME/.local/bin/xagent\" <<'EOF'",
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    'if [ \"${1:-}\" = \"version\" ]; then',
                    '  echo \"xAgent 9.9.9\"',
                    '  exit 0',
                    "fi",
                    'if [ \"${1:-}\" = \"processes\" ] && [ \"${2:-}\" = \"status\" ]; then',
                    '  echo \'{\"processes\":[{\"scope\":\"web\",\"status\":\"running\",\"pid\":1234}],\"running_count\":1}\'',
                    "  exit 2",
                    "fi",
                    'if [ \"${1:-}\" = \"processes\" ] && [ \"${2:-}\" = \"restart\" ]; then',
                    '  echo restarted >> \"$HOME/xagent_restart.log\"',
                    "  exit 0",
                    "fi",
                    "exit 0",
                    "EOF",
                    '  chmod +x "$HOME/.local/bin/xagent"',
                    "fi",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        fake_uv.chmod(0o755)

        fake_curl = fake_bin / "curl"
        fake_curl.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    'for arg in "$@"; do',
                    '  if [[ "$arg" == *"/pypi/myxagent/json" ]]; then',
                    '    echo \'{"info":{"version":"9.9.9"}}\'',
                    "    exit 0",
                    "  fi",
                    "done",
                    'exec /usr/bin/curl "$@"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        fake_curl.chmod(0o755)

        preinstalled = home / ".local" / "bin"
        preinstalled.mkdir(parents=True)
        preinstalled_xagent = preinstalled / "xagent"
        preinstalled_xagent.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    'if [ "${1:-}" = "version" ]; then',
                    '  echo "xAgent 1.0.0"',
                    "  exit 0",
                    "fi",
                    'if [ "${1:-}" = "processes" ] && [ "${2:-}" = "status" ]; then',
                    '  echo \'{"processes":[{"scope":"web","status":"running","pid":1234}],"running_count":1}\'',
                    "  exit 2",
                    "fi",
                    'if [ "${1:-}" = "processes" ] && [ "${2:-}" = "restart" ]; then',
                    '  echo restarted >> "$HOME/xagent_restart.log"',
                    "  exit 0",
                    "fi",
                    "exit 0",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        preinstalled_xagent.chmod(0o755)

        return fake_bin, preinstalled

    def test_upgrade_warns_when_running_processes_detected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            fake_bin, preinstalled = self._write_fake_upgrade_tools(home)

            env = {
                **os.environ,
                "HOME": str(home),
                "PATH": f"{fake_bin}:{preinstalled}:/usr/bin:/bin:/usr/sbin:/sbin",
                "XAGENT_NO_PATH_MODIFY": "1",
            }
            result = subprocess.run(
                ["bash", str(INSTALL_SCRIPT)],
                check=True,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertIn("background services are still running", result.stdout)
            self.assertIn("xagent processes restart", result.stdout)
            self.assertIn("After upgrading, restart running services:", result.stdout)
            self.assertFalse((home / "xagent_restart.log").exists())

    def test_upgrade_auto_restart_when_requested(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            fake_bin, preinstalled = self._write_fake_upgrade_tools(home)

            env = {
                **os.environ,
                "HOME": str(home),
                "PATH": f"{fake_bin}:{preinstalled}:/usr/bin:/bin:/usr/sbin:/sbin",
                "XAGENT_NO_PATH_MODIFY": "1",
                "XAGENT_AUTO_RESTART": "1",
            }
            result = subprocess.run(
                ["bash", str(INSTALL_SCRIPT)],
                check=True,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertIn("Restarting running services", result.stdout)
            self.assertTrue((home / "xagent_restart.log").exists())
            self.assertIn("restarted", (home / "xagent_restart.log").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
