import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from xagent.core.runtime.ownership import RuntimeOwnership, runtime_paths, write_runtime_status
from xagent.core.runtime.tasks import AsyncTaskScheduler, enqueue_scheduled_task
from xagent.interfaces.cli.launcher import _launcher_options, _run_runtime_launcher
from xagent.interfaces.cli.overview import build_runtime_overview, runtime_snapshot


class LauncherRuntimeTests(unittest.TestCase):
    def prepare(self, root):
        (root / "config.yaml").write_text(yaml.safe_dump({
            "provider": {"name": "openai", "api_key": "test-key", "model": "test-model"},
            "channels": {"api": {"enabled": True, "host": "127.0.0.1", "port": 8010}},
        }), encoding="utf-8")
        (root / "identity.md").write_text("I am an agent.", encoding="utf-8")

    def test_runtime_control_menu_starts_all_configured_channels(self):
        class UI:
            choices = iter((SimpleNamespace(key="start"), SimpleNamespace(key="back")))
            def select_menu(self, **kwargs):
                self.keys = [option.key for option in kwargs["options"]]
                return next(self.choices)
            def clear(self): pass
            def pause(self, *_args): pass
            def print_panel(self, *_args, **_kwargs): raise AssertionError("unexpected error")
        ui = UI()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("xagent.interfaces.cli.launcher.TerminalUI", return_value=ui):
                with patch("xagent.interfaces.cli.launcher.handle_start", return_value=0) as start:
                    self.assertEqual(_run_runtime_launcher(root), 0)
        self.assertEqual(ui.keys, ["start", "stop", "restart", "status", "logs", "back"])
        self.assertIsNone(start.call_args.args[0].channels)
        self.assertIn("runtime", [option.key for option in _launcher_options(initialized=True)])

    def test_stale_status_cannot_claim_running_without_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_runtime_status(runtime_paths(root), {"state": "running", "runtime_ready": True,
                                                      "channels": {"api": {"state": "running"}}})
            self.assertEqual(runtime_snapshot(root)["state"], "stopped")

    def test_overview_shows_degraded_runtime_and_uncertain_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            task = enqueue_scheduled_task(task_type="agent", content="check", run_at="2026-06-01 10:00:00",
                                          tasks_dir=root / "tasks", channel="api", target={"user_id": "alice"})
            task.path.rename(task.path.with_name(task.path.name + ".running-old"))
            scheduler = AsyncTaskScheduler(root / "tasks", can_handle=lambda _: True,
                                           dispatch=lambda _: None)
            scheduler.recover_running_tasks()
            with RuntimeOwnership(root):
                write_runtime_status(runtime_paths(root), {"state": "degraded", "runtime_ready": True,
                    "channels": {"api": {"state": "running"}, "feishu": {"state": "failed", "error": "check log"}}})
                overview = build_runtime_overview(root)
            rows = {item.name: item for item in overview.items}
            self.assertEqual(rows["Runtime"].value, "degraded")
            self.assertEqual(rows["Runtime"].status, "warning")
            self.assertEqual(rows["API"].value, "running")
            self.assertEqual(rows["Feishu"].value, "failed")
            self.assertEqual(rows["Tasks"].value, "1 need review")
            self.assertEqual(rows["Tasks"].status, "warning")
