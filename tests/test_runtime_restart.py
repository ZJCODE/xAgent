"""Live connection intent, restart isolation, and the private startup handoff."""
import argparse
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import yaml

from xagent.core.runtime.client import RuntimeUnavailable
from xagent.core.runtime.ownership import runtime_paths, write_runtime_status
from xagent.interfaces.cli import agent_runtime
from xagent.interfaces.cli.parser import build_parser


class RuntimeRestartTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    async def restart(self, before, *, acknowledged=None, channels=None, active=True, after=None):
        stop_result = {"status": "stopped"}
        if acknowledged is not None:
            stop_result["restart_channels"] = acknowledged
        with patch.object(agent_runtime, "runtime_is_active", return_value=active), patch(
            "xagent.core.runtime.client.RuntimeClient.status", new_callable=AsyncMock,
            return_value={"channels": before},
        ) as status, patch.object(
            agent_runtime, "stop_runtime", new_callable=AsyncMock, return_value=stop_result,
        ) as stop, patch.object(
            agent_runtime, "start_runtime", return_value=after or {"status": "running", "channels": {}},
        ) as start:
            result = await agent_runtime.restart_runtime(self.root, channels)
        stop.assert_awaited_once_with(self.root, restart=True)
        return result, start, status

    async def test_restores_connection_intent_for_every_channel(self):
        for state in ("connected", "connecting", "reconnecting", "running"):
            with self.subTest(state=state):
                before = {name: {"state": state} for name in ("api", "feishu", "weixin", "voice")}
                result, start, _ = await self.restart(before)
                start.assert_called_once_with(self.root, ["api", "feishu", "weixin", "voice"])
                self.assertNotIn("restart_warnings", result)

    async def test_closed_failed_and_disconnecting_channels_stay_offline(self):
        for state in ("disconnected", "disconnecting", "failed", "error", "starting"):
            with self.subTest(state=state):
                _, start, _ = await self.restart({"api": {"state": state}, "weixin": {"state": "connected"}})
                start.assert_called_once_with(self.root, ["weixin"])

    async def test_atomic_stop_snapshot_overrides_earlier_live_status(self):
        _, start, _ = await self.restart(
            {"api": {"state": "connected"}, "weixin": {"state": "connected"}}, acknowledged=["weixin"],
        )
        start.assert_called_once_with(self.root, ["weixin"])

    async def test_empty_snapshot_does_not_fall_back_to_default_api(self):
        for acknowledged in (None, []):
            with self.subTest(acknowledged=acknowledged):
                _, start, _ = await self.restart({"api": {"state": "disconnected"}}, acknowledged=acknowledged)
                start.assert_called_once_with(self.root, [])

    async def test_newly_disabled_channels_are_skipped_after_stop(self):
        async def stop(root, *, restart=False):
            (root / "config.yaml").write_text(yaml.safe_dump({"channels": {
                "api": {"enabled": False}, "weixin": {"enabled": False}, "voice": {"enabled": True},
            }}))
            return {"restart_channels": ["api", "weixin", "voice"]}
        with patch.object(agent_runtime, "runtime_is_active", return_value=True), patch(
            "xagent.core.runtime.client.RuntimeClient.status", new_callable=AsyncMock, return_value={"channels": {}},
        ), patch.object(agent_runtime, "stop_runtime", side_effect=stop), patch.object(
            agent_runtime, "start_runtime", return_value={"status": "running", "channels": {}},
        ) as start:
            result = await agent_runtime.restart_runtime(self.root)
        start.assert_called_once_with(self.root, ["voice"])
        self.assertEqual(len(result["restart_warnings"]), 2)
        self.assertTrue(all("disabled" in warning for warning in result["restart_warnings"]))

    async def test_explicit_selection_overrides_snapshot_and_disabled_configuration(self):
        (self.root / "config.yaml").write_text("channels:\n  voice:\n    enabled: false\n")
        result, start, status = await self.restart(
            {"api": {"state": "connected"}}, acknowledged=["api"], channels=["voice"],
        )
        start.assert_called_once_with(self.root, ["voice"])
        status.assert_not_awaited()
        self.assertNotIn("restart_warnings", result)

    async def test_stopped_agent_uses_default_startup(self):
        result, start, status = await self.restart({}, active=False)
        start.assert_called_once_with(self.root, None)
        status.assert_not_awaited()
        self.assertNotIn("restart_warnings", result)

    async def test_unavailable_status_uses_acknowledgement_not_cached_status(self):
        write_runtime_status(runtime_paths(self.root), {"channels": {"voice": {"state": "connected"}}})
        for acknowledgement, selected in (({}, None), ({"restart_channels": ["weixin"]}, ["weixin"])):
            with self.subTest(acknowledgement=acknowledgement), patch.object(
                agent_runtime, "runtime_is_active", return_value=True,
            ), patch("xagent.core.runtime.client.RuntimeClient.status", side_effect=RuntimeUnavailable("offline")), patch.object(
                agent_runtime, "stop_runtime", new_callable=AsyncMock, return_value=acknowledgement,
            ), patch.object(agent_runtime, "start_runtime", return_value={"channels": {}}) as start:
                result = await agent_runtime.restart_runtime(self.root)
                start.assert_called_once_with(self.root, selected)
                self.assertEqual("restart_warnings" in result, selected is None)

    async def test_signal_recovery_waits_for_ownership_and_warns_without_snapshot(self):
        with patch.object(agent_runtime, "runtime_is_active", side_effect=[True, False]), patch(
            "xagent.core.runtime.client.RuntimeClient.status", side_effect=RuntimeUnavailable("offline"),
        ), patch.object(agent_runtime, "stop_runtime", side_effect=RuntimeUnavailable("offline")), patch.object(
            agent_runtime, "stop_managed_process", return_value=(True, "stopped"),
        ) as signal_stop, patch.object(agent_runtime, "start_runtime", return_value={"channels": {}}) as start:
            result = await agent_runtime.restart_runtime(self.root)
        signal_stop.assert_called_once_with(runtime_paths(self.root).pid_path)
        start.assert_called_once_with(self.root, None)
        self.assertIn("default connections", result["restart_warnings"][0])

    async def test_signal_recovery_cannot_start_while_owner_is_still_active(self):
        with patch.object(agent_runtime, "runtime_is_active", return_value=True), patch(
            "xagent.core.runtime.client.RuntimeClient.status", side_effect=RuntimeUnavailable("offline"),
        ), patch.object(agent_runtime, "stop_runtime", side_effect=RuntimeUnavailable("offline")), patch.object(
            agent_runtime, "stop_managed_process", return_value=(True, "stopped"),
        ), patch.object(agent_runtime, "start_runtime") as start:
            with self.assertRaisesRegex(RuntimeError, "ownership"):
                await agent_runtime.restart_runtime(self.root)
        start.assert_not_called()

    async def test_failed_shutdown_or_signal_stop_never_starts_replacement(self):
        for error in (RuntimeError("still draining"), RuntimeUnavailable("offline")):
            with self.subTest(error=error), patch.object(agent_runtime, "runtime_is_active", return_value=True), patch(
                "xagent.core.runtime.client.RuntimeClient.status", return_value={"channels": {}},
            ), patch.object(agent_runtime, "stop_runtime", side_effect=error), patch.object(
                agent_runtime, "stop_managed_process", return_value=(False, "could not stop"),
            ) as signal_stop, patch.object(agent_runtime, "start_runtime") as start:
                with self.assertRaises(RuntimeError):
                    await agent_runtime.restart_runtime(self.root)
                start.assert_not_called()
                self.assertEqual(signal_stop.called, isinstance(error, RuntimeUnavailable))

    async def test_channel_failure_is_reported_without_failing_runtime_restart(self):
        result, start, _ = await self.restart({}, acknowledged=["weixin"], after={
            "status": "degraded", "channels": {"weixin": {"state": "failed"}, "api": {"state": "connected"}},
        })
        self.assertEqual(result["status"], "degraded")
        self.assertIn("weixin", result["restart_warnings"][0])
        start.assert_called_once()

    async def test_stop_preserves_atomic_snapshot_until_ownership_is_released(self):
        with patch.object(agent_runtime, "runtime_is_active", side_effect=[True, False]), patch(
            "xagent.core.runtime.client.RuntimeClient.stop", return_value={"stopping": True, "restart_channels": []},
        ):
            result = await agent_runtime.stop_runtime(self.root, restart=True)
        self.assertFalse(result["runtime_running"])
        self.assertEqual(result["restart_channels"], [])


class RuntimeStartupHandoffTests(unittest.TestCase):
    def test_empty_selection_survives_child_command_and_parser(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch.object(agent_runtime, "runtime_is_active", side_effect=[False, True]), patch.object(
                agent_runtime, "_runtime_ready", return_value=True,
            ), patch.object(agent_runtime, "runtime_status", new_callable=AsyncMock, return_value={"status": "running"}), patch.object(
                agent_runtime.subprocess, "Popen", return_value=Mock(),
            ) as launch:
                agent_runtime.start_runtime(root, [])
            command = launch.call_args.args[0]
            self.assertIn("--no-channels", command)
            self.assertNotIn("--channels", command)
            parsed = build_parser().parse_args(command[3:])
            self.assertEqual(agent_runtime.selected_channels(parsed), [])
            self.assertIsNone(agent_runtime.selected_channels(build_parser().parse_args(["start"])))

    def test_private_empty_and_explicit_selection_are_mutually_exclusive(self):
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["_run-agent", "--no-channels", "--channels", "api"])

    def test_cli_restart_uses_shared_flow_and_displays_warnings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            args = argparse.Namespace(config_dir=str(root), channels=["weixin,voice"])
            with patch.object(agent_runtime, "restart_runtime", new_callable=AsyncMock, return_value={
                "channels": {"weixin": {"state": "connecting"}}, "restart_warnings": ["voice: connection failed"],
            }) as restart, patch("sys.stdout", io.StringIO()) as output:
                self.assertEqual(agent_runtime.handle_restart(args), 0)
            restart.assert_awaited_once_with(root, ["weixin", "voice"])
            self.assertIn("weixin: connecting", output.getvalue())
            self.assertIn("voice: connection failed", output.getvalue())
