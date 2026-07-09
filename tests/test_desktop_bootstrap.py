"""Tests for the Electron desktop bootstrap helpers."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = REPO_ROOT / "desktop" / "electron" / "bootstrap.cjs"


def _run_bootstrap(expression: str, *, env: dict[str, str] | None = None) -> object:
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    completed = subprocess.run(
        ["node", "-e", f"const bootstrap = require({json.dumps(str(BOOTSTRAP))}); {expression}"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        env=run_env,
    )
    return json.loads(completed.stdout.strip() or "null")


class DesktopBootstrapTests(unittest.TestCase):
    def test_install_command_uses_official_install_script(self):
        value = _run_bootstrap("console.log(JSON.stringify(bootstrap.INSTALL_COMMAND))")
        self.assertIn("curl -fsSL", value)
        self.assertIn("install.sh", value)

    def test_default_web_start_timeout_is_six_seconds(self):
        value = _run_bootstrap("console.log(JSON.stringify(bootstrap.DEFAULT_TIMEOUT_MS))")
        self.assertEqual(value, 6000)

    def test_configured_xagent_command_reads_manifest_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            python = home / "Python Bin" / "python3"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\n")
            manifest = home / ".xagent" / "cli.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({"command": [str(python), "-m", "xagent"]}))

            value = _run_bootstrap(
                "console.log(JSON.stringify(bootstrap.configuredXagentCommand()))",
                env={"HOME": str(home), "USERPROFILE": str(home)},
            )
            self.assertEqual(value, [str(python), "-m", "xagent"])

    def test_configured_xagent_command_returns_null_without_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = _run_bootstrap(
                "console.log(JSON.stringify(bootstrap.configuredXagentCommand()))",
                env={"HOME": tmp, "USERPROFILE": tmp, "XAGENT_BINARY": ""},
            )
            self.assertIsNone(value)

    def test_configured_xagent_command_honors_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            override = home / "custom-xagent"
            override.write_text("#!/bin/sh\n")
            value = _run_bootstrap(
                "console.log(JSON.stringify(bootstrap.configuredXagentCommand()))",
                env={"HOME": str(home), "USERPROFILE": str(home), "XAGENT_BINARY": str(override)},
            )
            self.assertEqual(value, [str(override)])

    def test_resolve_xagent_command_appends_web_start_to_manifest_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            python = home / "Python Bin" / "python3"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\n")
            manifest = home / ".xagent" / "cli.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({"command": [str(python), "-m", "xagent"]}))

            command = _run_bootstrap(
                "console.log(JSON.stringify(bootstrap.resolveXagentCommand()))",
                env={"HOME": str(home), "USERPROFILE": str(home)},
            )
            self.assertEqual(command, [str(python), "-m", "xagent", "client", "web", "start"])

    def test_resolve_xagent_command_null_without_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = _run_bootstrap(
                "console.log(JSON.stringify(bootstrap.resolveXagentCommand()))",
                env={"HOME": tmp, "USERPROFILE": tmp, "XAGENT_BINARY": ""},
            )
            self.assertIsNone(command)

    def test_child_process_env_bypasses_proxy_for_loopback_hosts(self):
        value = _run_bootstrap(
            """
            const originalUpper = process.env.NO_PROXY;
            const originalLower = process.env.no_proxy;
            process.env.NO_PROXY = 'example.com,127.0.0.1';
            delete process.env.no_proxy;
            const env = bootstrap.childProcessEnv();
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
              let shellCount = 0;
              const statuses = [];
              const fetchWebShell = async () => {
                shellCount += 1;
                return started;
              };
              const result = await bootstrap.runBootstrap('http://127.0.0.1:1415', {
                initialProbeMs: 1,
                timeoutMs: 1000,
                pollMs: 1,
                onStatus: (status) => statuses.push(status.title || status),
                fetchWebShell,
                resolveXagentCommand: () => ['/tmp/xagent', 'client', 'web', 'start'],
                startWebClient: (command) => {
                  started = true;
                  return command.length === 4;
                },
              });
              console.log(JSON.stringify({ result, shellCount, statuses }));
            })().catch((error) => {
              console.error(error);
              process.exit(1);
            });
            """
        )
        self.assertEqual(value["result"], {"ok": True})
        self.assertGreaterEqual(value["shellCount"], 1)
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
                fetchWebShell: async () => false,
                resolveXagentCommand: () => null,
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

    def test_run_bootstrap_reports_web_timeout_when_shell_never_loads(self):
        value = _run_bootstrap(
            """
            (async () => {
              let startWebCalled = false;
              const result = await bootstrap.runBootstrap('http://127.0.0.1:1415', {
                initialProbeMs: 5,
                timeoutMs: 20,
                pollMs: 1,
                fetchWebShell: async () => false,
                resolveXagentCommand: () => ['/tmp/xagent', 'client', 'web', 'start'],
                startWebClient: () => {
                  startWebCalled = true;
                  return true;
                },
              });
              console.log(JSON.stringify({ result, startWebCalled }));
            })().catch((error) => {
              console.error(error);
              process.exit(1);
            });
            """
        )
        self.assertEqual(value["result"], {"ok": False, "reason": "web-timeout"})
        self.assertTrue(value["startWebCalled"])

    def test_run_bootstrap_enters_when_web_shell_loads(self):
        value = _run_bootstrap(
            """
            (async () => {
              let startWebCalled = false;
              const result = await bootstrap.runBootstrap('http://127.0.0.1:1415', {
                initialProbeMs: 1,
                pollMs: 1,
                fetchWebShell: async () => true,
                resolveXagentCommand: () => ['/tmp/xagent', 'client', 'web', 'start'],
                startWebClient: () => {
                  startWebCalled = true;
                  return true;
                },
              });
              console.log(JSON.stringify({ result, startWebCalled }));
            })().catch((error) => {
              console.error(error);
              process.exit(1);
            });
            """
        )
        self.assertEqual(value["result"], {"ok": True})
        self.assertFalse(value["startWebCalled"])


if __name__ == "__main__":
    unittest.main()
