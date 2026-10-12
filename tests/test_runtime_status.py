"""Expected shutdown transport loss is a lifecycle state, not a UI error."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from xagent.core.runtime.client import RuntimeUnavailable
from xagent.core.runtime.ownership import runtime_paths, write_runtime_status
from xagent.interfaces.cli.agent_runtime import runtime_status, stop_runtime


class RuntimeStatusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.unavailable = RuntimeUnavailable(f"Agent runtime is unavailable: {self.root}")

    async def test_shutdown_socket_loss_has_no_error(self):
        for state in ("stopping", "stopped"):
            with self.subTest(state=state):
                write_runtime_status(runtime_paths(self.root), {"state": state, "error": "stale error", "channels": {}})
                with patch("xagent.interfaces.cli.agent_runtime.runtime_is_active", return_value=True), patch(
                    "xagent.core.runtime.client.RuntimeClient.status", new_callable=AsyncMock, side_effect=self.unavailable
                ):
                    status = await runtime_status(self.root)
                self.assertEqual(status["status"], "stopping")
                self.assertFalse(status["runtime_ready"])
                self.assertNotIn("error", status)

    async def test_status_probe_racing_finished_shutdown_reports_offline(self):
        with patch("xagent.interfaces.cli.agent_runtime.runtime_is_active", side_effect=[True, False]), patch(
            "xagent.core.runtime.client.RuntimeClient.status", new_callable=AsyncMock, side_effect=self.unavailable
        ):
            status = await runtime_status(self.root)
        self.assertEqual(status["status"], "stopped")
        self.assertFalse(status["runtime_running"])
        self.assertNotIn("error", status)

    async def test_unexpected_running_transport_loss_still_reports_error(self):
        write_runtime_status(runtime_paths(self.root), {"state": "running", "channels": {}})
        with patch("xagent.interfaces.cli.agent_runtime.runtime_is_active", return_value=True), patch(
            "xagent.core.runtime.client.RuntimeClient.status", new_callable=AsyncMock, side_effect=self.unavailable
        ):
            status = await runtime_status(self.root)
        self.assertEqual(status["error"], str(self.unavailable))

    async def test_stop_tolerates_socket_closing_before_acknowledgement(self):
        with patch("xagent.interfaces.cli.agent_runtime.runtime_is_active", side_effect=[True, False]), patch(
            "xagent.core.runtime.client.RuntimeClient.stop", new_callable=AsyncMock, side_effect=self.unavailable
        ):
            status = await stop_runtime(self.root)
        self.assertEqual(status["status"], "stopped")

    async def test_lost_stop_acknowledgement_waits_for_ownership_release(self):
        with patch("xagent.interfaces.cli.agent_runtime.runtime_is_active", side_effect=[True, True, False]), patch(
            "xagent.interfaces.cli.agent_runtime.runtime_status", new_callable=AsyncMock,
            return_value={"status": "stopping", "runtime_running": True}
        ), patch("xagent.core.runtime.client.RuntimeClient.stop", new_callable=AsyncMock, side_effect=self.unavailable), patch(
            "xagent.interfaces.cli.agent_runtime.asyncio.sleep", new_callable=AsyncMock
        ):
            status = await stop_runtime(self.root)
        self.assertFalse(status["runtime_running"])

    async def test_real_stop_failure_is_not_suppressed(self):
        with patch("xagent.interfaces.cli.agent_runtime.runtime_is_active", return_value=True), patch(
            "xagent.interfaces.cli.agent_runtime.runtime_status", new_callable=AsyncMock,
            return_value={"status": "running", "runtime_running": True}
        ), patch("xagent.core.runtime.client.RuntimeClient.stop", new_callable=AsyncMock, side_effect=self.unavailable):
            with self.assertRaises(RuntimeUnavailable):
                await stop_runtime(self.root)
