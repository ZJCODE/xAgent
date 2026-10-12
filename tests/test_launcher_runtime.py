import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import yaml

from xagent.core.runtime.ownership import RuntimeOwnership, runtime_paths, write_runtime_status
from xagent.core.runtime.tasks import AsyncTaskScheduler, enqueue_scheduled_task
from xagent.interfaces.cli.launcher import (
    _channel_runtime_snapshot,
    _launcher_channel_options,
    _launcher_options,
    _managed_channel_actions,
    _run_managed_channel_action,
    _run_managed_channel_launcher,
    _run_runtime_launcher,
    _run_voice_channel_launcher,
    _voice_channel_options,
)
from xagent.interfaces.cli.overview import build_runtime_overview, runtime_snapshot


class LauncherRuntimeTests(unittest.TestCase):
    def prepare(self, root):
        (root / "config.yaml").write_text(yaml.safe_dump({
            "provider": {"name": "openai", "api_key": "test-key", "model": "test-model"},
            "channels": {"api": {"enabled": True, "host": "127.0.0.1", "port": 8010}},
        }), encoding="utf-8")
        (root / "identity.md").write_text("I am an agent.", encoding="utf-8")

    def test_runtime_control_menu_uses_default_api_startup(self):
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


class ChannelMenuTests(unittest.TestCase):
    def test_refresh_reads_current_owner_capabilities_instead_of_stale_file(self):
        root = Path("/tmp/menu-agent")
        cached = {"state": "running", "runtime_ready": True, "channels": {
            "api": {"state": "disconnected", "ready": True, "can_connect": False},
        }}
        current = {**cached, "status": "running", "needs_restart": True, "channels": {
            "api": {"state": "disconnected", "ready": True, "can_connect": True},
        }}
        with patch("xagent.interfaces.cli.launcher.runtime_snapshot", return_value=cached):
            with patch("xagent.interfaces.cli.launcher.runtime_status", new=AsyncMock(return_value=current)) as status:
                self.assertTrue(_channel_runtime_snapshot(root)["channels"]["api"]["can_connect"])
                status.assert_awaited_once_with(root)

    def test_stopped_menu_preview_does_not_request_or_start_runtime(self):
        root = Path("/tmp/menu-agent")
        stopped = {"state": "stopped", "runtime_ready": False, "channels": {}}
        with patch("xagent.interfaces.cli.launcher.runtime_snapshot", return_value=stopped):
            with patch("xagent.interfaces.cli.launcher.runtime_status") as status:
                self.assertEqual(_channel_runtime_snapshot(root), stopped)
                status.assert_not_called()

    def snapshot(self, channel, state, *, ready=True, connect=False, disconnect=False, **extra):
        return {"state": "running", "runtime_ready": True, "channels": {
            channel: {"state": state, "ready": ready, "can_connect": connect,
                      "can_disconnect": disconnect, **extra},
        }}

    def actions(self, root, channel, runtime):
        with patch("xagent.interfaces.cli.launcher._channel_runtime_snapshot", return_value=runtime):
            if channel == "voice":
                return {row.key: row for row in _voice_channel_options({}, root, runtime)}
            return {row.key: row for row in _managed_channel_actions(root, channel)}

    def test_every_channel_uses_runtime_capabilities_for_connection_controls(self):
        root = Path("/tmp/menu-agent")
        cases = (
            ("disconnected", True, True, False, "connect", False),
            ("disconnected", False, False, False, "connect", True),
            ("connecting", True, False, True, "disconnect", False),
            ("connected", True, False, True, "disconnect", False),
            ("reconnecting", True, False, True, "disconnect", False),
            ("disconnecting", True, False, False, "disconnect", True),
            ("failed", True, True, False, "connect", False),
            ("failed", True, False, True, "disconnect", False),
        )
        for channel in ("api", "feishu", "weixin", "voice"):
            for state, ready, connect, disconnect, key, disabled in cases:
                with self.subTest(channel=channel, state=state, cleanup=disconnect):
                    actions = self.actions(root, channel, self.snapshot(
                        channel, state, ready=ready, connect=connect, disconnect=disconnect,
                    ))
                    self.assertEqual(actions[key].disabled, disabled)
                    self.assertNotIn("connect" if key == "disconnect" else "disconnect", actions)
                    setup = actions["setup" if channel in {"feishu", "weixin"} else "configure"]
                    self.assertFalse(setup.disabled)

    def test_unavailable_runtime_disables_operations_even_with_stale_capabilities(self):
        for agent_state in ("stopped", "starting", "stopping"):
            for channel in ("api", "feishu", "weixin", "voice"):
                with self.subTest(agent_state=agent_state, channel=channel):
                    runtime = self.snapshot(channel, "connected", connect=True, disconnect=True)
                    runtime.update(state=agent_state, runtime_ready=False)
                    actions = self.actions(Path("/tmp/menu-agent"), channel, runtime)
                    control = actions.get("connect") or actions["disconnect"]
                    self.assertTrue(control.disabled)
                    if agent_state == "stopped":
                        self.assertIn("Start the Agent", control.description)

    def test_running_configuration_is_authoritative_even_after_saved_config_changes(self):
        runtime = self.snapshot("voice", "disconnected", ready=False, connect=False,
                                config_error="Configure voice before connecting")
        with patch("xagent.interfaces.cli.launcher.channel_readiness",
                   side_effect=AssertionError("must use running configuration")):
            actions = self.actions(Path("/tmp/menu-agent"), "voice", runtime)
        self.assertTrue(actions["connect"].disabled)
        self.assertIn("Configure voice", actions["connect"].description)

    def test_list_displays_channel_states_instead_of_inferring_connection_from_agent(self):
        runtime = {"state": "running", "runtime_ready": True, "channels": {
            "api": {"state": "connected", "ready": True, "can_disconnect": True},
            "feishu": {"state": "disconnected", "ready": True, "can_connect": True},
            "weixin": {"state": "disconnected", "ready": False, "config_error": "Run setup"},
            "voice": {"state": "failed", "ready": True, "can_disconnect": True},
        }}
        with patch("xagent.interfaces.cli.launcher._channel_runtime_snapshot", return_value=runtime):
            options = {row.key: row for row in _launcher_channel_options(Path("/tmp/menu-agent"))}
        self.assertEqual(list(options), ["chat", "api", "feishu", "weixin", "voice", "back"])
        self.assertEqual(options["api"].badge, "● Connected")
        self.assertEqual(options["api"].badge_style, "bold green")
        self.assertIn("Disconnected", options["feishu"].badge)
        self.assertIn("Needs configuration", options["weixin"].badge)
        self.assertIn("Failed", options["voice"].badge)
        self.assertEqual(options["voice"].badge_style, "bold red")
        self.assertIn("Retry cleanup", options["voice"].description)

    def test_degraded_agent_can_still_connect_healthy_channel(self):
        runtime = self.snapshot("api", "disconnected", connect=True)
        runtime["state"] = "degraded"
        self.assertFalse(self.actions(Path("/tmp/menu-agent"), "api", runtime)["connect"].disabled)

    def test_menu_actions_pin_agent_directory_and_reuse_command_client(self):
        # An external client can change the registry while this menu stays open.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = SimpleNamespace(
                connect_channel=AsyncMock(return_value={"channel": {"state": "connecting"}}),
                disconnect_channel=AsyncMock(return_value={"channel": {"state": "disconnected"}}),
            )
            with patch("xagent.interfaces.cli.agent_runtime.RuntimeClient", return_value=client) as factory:
                with patch("xagent.interfaces.cli.paths.resolve_agent_runtime_dir",
                           side_effect=AssertionError("must not follow active Agent changes")):
                    for channel in ("api", "feishu", "weixin", "voice"):
                        for action in ("connect", "disconnect"):
                            self.assertEqual(_run_managed_channel_action(root, channel, action), 0)
                            self.assertEqual(factory.call_args.args[0], root.resolve())
                            method = client.connect_channel if action == "connect" else client.disconnect_channel
                            self.assertEqual(method.await_args.args, (channel,))

    def test_menus_refresh_after_operations_and_voice_does_not_start_foreground_audio(self):
        for channel in ("api", "feishu", "weixin", "voice"):
            with self.subTest(channel=channel):
                snapshots = [self.snapshot(channel, "disconnected", connect=True),
                             self.snapshot(channel, "connecting", disconnect=True),
                             self.snapshot(channel, "disconnected", connect=True)]
                choices = iter(("connect", "disconnect", "back"))
                subtitles = []

                class UI:
                    def select_menu(self, **kwargs):
                        subtitles.append(kwargs["subtitle"])
                        options = {row.key: row for row in kwargs["options"]}
                        choice = options[next(choices)]
                        self_outer.assertFalse(choice.disabled)
                        return choice
                    def clear(self): pass
                    def pause(self, *_args): pass
                    def print_panel(self, *_args, **_kwargs): raise AssertionError("unexpected error")

                self_outer = self
                root = Path("/tmp/menu-agent")
                with patch("xagent.interfaces.cli.launcher._channel_runtime_snapshot", side_effect=snapshots):
                    with patch("xagent.interfaces.cli.launcher.handle_channel_connection", return_value=0) as control:
                        with patch("xagent.interfaces.cli.launcher.handle_voice") as voice:
                            if channel == "voice":
                                _run_voice_channel_launcher(UI(), root)
                            else:
                                _run_managed_channel_launcher(UI(), root, channel, channel_title=channel.title())
                self.assertEqual([call.args[0].channel_action for call in control.call_args_list],
                                 ["connect", "disconnect"])
                self.assertTrue(all(call.args[0].channel == channel and call.args[0].config_dir == str(root)
                                    for call in control.call_args_list))
                voice.assert_not_called()
                self.assertIn("Channel: Connecting", subtitles[1])
                self.assertIn("Channel: Disconnected", subtitles[2])

    def test_refresh_exposes_failed_cleanup_and_operation_error_keeps_menu_available(self):
        for channel in ("feishu", "voice"):
            with self.subTest(channel=channel):
                snapshots = [self.snapshot(channel, "connected", disconnect=True),
                             self.snapshot(channel, "failed", disconnect=True, error="Cleanup timed out"),
                             self.snapshot(channel, "failed", disconnect=True, error="Cleanup timed out")]
                choices = iter(("refresh", "disconnect", "back"))
                panels = []
                subtitles = []

                class UI:
                    def select_menu(self, **kwargs):
                        subtitles.append(kwargs["subtitle"])
                        key = next(choices)
                        return next(row for row in kwargs["options"] if row.key == key)
                    def clear(self): pass
                    def pause(self, *_args): pass
                    def print_panel(self, *args, **_kwargs): panels.append(args[0])

                ui = UI()
                with patch("xagent.interfaces.cli.launcher._channel_runtime_snapshot", side_effect=snapshots):
                    with patch("xagent.interfaces.cli.launcher.handle_channel_connection", return_value=1) as control:
                        if channel == "voice":
                            _run_voice_channel_launcher(ui, Path("/tmp/menu-agent"))
                        else:
                            _run_managed_channel_launcher(ui, Path("/tmp/menu-agent"), channel, channel_title="Feishu")
                control.assert_called_once()
                self.assertEqual(control.call_args.args[0].channel_action, "disconnect")
                self.assertEqual(len(panels), 1)
                self.assertIn("Cleanup timed out", subtitles[1])
