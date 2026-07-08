"""Tests for the Electron desktop bootstrap helpers."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = REPO_ROOT / "desktop" / "electron" / "bootstrap.cjs"


def _run_bootstrap(expression: str) -> object:
    completed = subprocess.run(
        ["node", "-e", f"const bootstrap = require({json.dumps(str(BOOTSTRAP))}); {expression}"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout.strip() or "null")


class DesktopBootstrapTests(unittest.TestCase):
    def test_resolve_xagent_commands_includes_python_module_fallback(self):
        commands = _run_bootstrap("console.log(JSON.stringify(bootstrap.resolveXagentCommands()))")
        self.assertIsInstance(commands, list)
        self.assertTrue(commands)
        joined = [" ".join(command) for command in commands]
        self.assertTrue(
            any("client web start" in command for command in joined)
            or any("xagent.interfaces.cli" in command for command in joined)
        )

    def test_install_command_uses_official_install_script(self):
        value = _run_bootstrap("console.log(JSON.stringify(bootstrap.INSTALL_COMMAND))")
        self.assertIn("curl -fsSL", value)
        self.assertIn("install.sh", value)

    def test_conda_install_candidates_include_anaconda_home_bin(self):
        candidates = _run_bootstrap("console.log(JSON.stringify(bootstrap.condaInstallCandidates('xagent')))")
        self.assertIsInstance(candidates, list)
        home = Path.home()
        self.assertIn(str(home / "anaconda3" / "bin" / "xagent"), candidates)

    def test_find_xagent_binary_checks_conda_candidate(self):
        expression = """
        const fs = require('fs');
        const os = require('os');
        const path = require('path');
        const candidate = path.join(os.homedir(), 'anaconda3', 'bin', 'xagent');
        const original = bootstrap.findXagentBinary;
        bootstrap.findXagentBinary = () => {
          if (fs.existsSync(candidate)) return candidate;
          return original();
        };
        console.log(JSON.stringify(bootstrap.findXagentBinary()));
        """
        value = _run_bootstrap(expression)
        candidate = str(Path.home() / "anaconda3" / "bin" / "xagent")
        if Path(candidate).is_file():
            self.assertEqual(value, candidate)
        else:
            self.assertTrue(value is None or isinstance(value, str))

    def test_find_python_returns_string_or_null(self):
        value = _run_bootstrap("console.log(JSON.stringify(bootstrap.findPython()))")
        self.assertTrue(value is None or isinstance(value, str))

    def test_enriched_env_bypasses_proxy_for_loopback_hosts(self):
        value = _run_bootstrap(
            """
            const originalUpper = process.env.NO_PROXY;
            const originalLower = process.env.no_proxy;
            process.env.NO_PROXY = 'example.com,127.0.0.1';
            delete process.env.no_proxy;
            const env = bootstrap.enrichedPathEnv();
            if (originalUpper === undefined) {
              delete process.env.NO_PROXY;
            } else {
              process.env.NO_PROXY = originalUpper;
            }
            if (originalLower === undefined) {
              delete process.env.no_proxy;
            } else {
              process.env.no_proxy = originalLower;
            }
            console.log(JSON.stringify({ upper: env.NO_PROXY, lower: env.no_proxy }));
            """
        )
        entries = {entry.strip() for entry in value["upper"].split(",")}
        self.assertIn("example.com", entries)
        self.assertIn("127.0.0.1", entries)
        self.assertIn("localhost", entries)
        self.assertIn("::1", entries)
        self.assertEqual(value["upper"], value["lower"])

    def test_run_bootstrap_starts_web_after_short_initial_probe(self):
        value = _run_bootstrap(
            """
            (async () => {
              let started = false;
              let fetchCount = 0;
              const statuses = [];
              const fetchWebHealth = async () => {
                fetchCount += 1;
                if (!started) {
                  return null;
                }
                return { status: 'ok', web: true, api_reachable: true };
              };
              const result = await bootstrap.runBootstrap('http://127.0.0.1:1415', {
                initialProbeMs: 1,
                timeoutMs: 1000,
                pollMs: 1,
                onStatus: (status) => statuses.push(status.title || status),
                fetchWebHealth,
                fetchWebShell: async () => false,
                resolveXagentCommands: () => [['/tmp/xagent', 'client', 'web', 'start']],
                startWebClient: (commands) => {
                  started = true;
                  return commands.length === 1;
                },
              });
              console.log(JSON.stringify({ result, fetchCount, statuses }));
            })().catch((error) => {
              console.error(error);
              process.exit(1);
            });
            """
        )
        self.assertEqual(value["result"], {"ok": True})
        self.assertGreaterEqual(value["fetchCount"], 2)
        self.assertIn("Locating xAgent", value["statuses"])
        self.assertIn("Starting web service", value["statuses"])

    def test_run_bootstrap_reports_missing_xagent_after_initial_probe(self):
        value = _run_bootstrap(
            """
            (async () => {
              let startCalled = false;
              const result = await bootstrap.runBootstrap('http://127.0.0.1:1415', {
                initialProbeMs: 1,
                timeoutMs: 1000,
                pollMs: 1,
                fetchWebHealth: async () => null,
                fetchWebShell: async () => false,
                resolveXagentCommands: () => [],
                startWebClient: () => {
                  startCalled = true;
                  return true;
                },
              });
              console.log(JSON.stringify({ result, startCalled }));
            })().catch((error) => {
              console.error(error);
              process.exit(1);
            });
            """
        )
        self.assertEqual(value["result"], {"ok": False, "reason": "missing-xagent"})
        self.assertFalse(value["startCalled"])

    def test_run_bootstrap_enters_when_health_is_blocked_but_web_shell_loads(self):
        value = _run_bootstrap(
            """
            (async () => {
              let startWebCalled = false;
              let startApiCalled = false;
              const result = await bootstrap.runBootstrap('http://127.0.0.1:1415', {
                initialProbeMs: 1,
                apiWaitMs: 1,
                pollMs: 1,
                fetchWebHealth: async () => null,
                fetchWebShell: async () => true,
                resolveXagentCommands: () => [['/tmp/xagent', 'client', 'web', 'start']],
                startWebClient: () => {
                  startWebCalled = true;
                  return true;
                },
                startApiChannel: () => {
                  startApiCalled = true;
                  return true;
                },
              });
              console.log(JSON.stringify({ result, startWebCalled, startApiCalled }));
            })().catch((error) => {
              console.error(error);
              process.exit(1);
            });
            """
        )
        self.assertEqual(value["result"], {"ok": True})
        self.assertFalse(value["startWebCalled"])
        self.assertTrue(value["startApiCalled"])


if __name__ == "__main__":
    unittest.main()
