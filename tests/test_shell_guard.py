import tempfile
import unittest
from pathlib import Path

from xagent.core.tooling.executor import ToolExecutor
from xagent.core.tooling.guards import (
    ToolCallContext,
    ToolDecision,
    ToolGuardResult,
    WorkspaceEscapeError,
    WorkspaceShellGuard,
    resolve_workspace_cwd,
)
from xagent.tools.shell_tool import create_workspace_run_command_tool

from tests.test_agent_chat_flow import FakeToolCall, FakeToolManager, InMemoryMessageStorage


class ResolveWorkspaceCwdTests(unittest.TestCase):
    def test_defaults_to_workspace_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertEqual(resolve_workspace_cwd(None, tmpdir), str(Path(tmpdir).resolve()))
            self.assertEqual(resolve_workspace_cwd("", tmpdir), str(Path(tmpdir).resolve()))

    def test_allows_relative_path_inside_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            nested = Path(tmpdir) / "notes"
            nested.mkdir()
            resolved = resolve_workspace_cwd("notes", tmpdir)
            self.assertEqual(resolved, str(nested.resolve()))

    def test_rejects_parent_escape(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(WorkspaceEscapeError):
                resolve_workspace_cwd("..", tmpdir)

    def test_rejects_absolute_path_outside_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(WorkspaceEscapeError):
                resolve_workspace_cwd("/tmp", tmpdir)


class WorkspaceShellGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_allows_non_shell_tools(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            guard = WorkspaceShellGuard(tmpdir)
            result = await guard.pre_execute(
                ToolCallContext(name="web_search", args={"query": "hi"}, workspace_dir=Path(tmpdir))
            )
            self.assertEqual(result.decision, ToolDecision.ALLOW)

    async def test_denies_escaped_shell_cwd(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            guard = WorkspaceShellGuard(tmpdir)
            result = await guard.pre_execute(
                ToolCallContext(
                    name="run_command",
                    args={"command": "pwd", "working_directory": "/tmp"},
                    workspace_dir=Path(tmpdir),
                )
            )
            self.assertEqual(result.decision, ToolDecision.DENY)
            self.assertIn("outside the agent workspace", result.reason)


class WorkspaceRunCommandToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_workspace_tool_rejects_escaped_cwd(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tool = create_workspace_run_command_tool(tmpdir)
            result = await tool(command="pwd", working_directory="/tmp")
            self.assertEqual(result["return_code"], -1)
            self.assertIn("outside the agent workspace", result["stderr"])

    async def test_workspace_tool_runs_inside_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            marker = Path(tmpdir) / "hello.txt"
            marker.write_text("ok", encoding="utf-8")
            tool = create_workspace_run_command_tool(tmpdir)
            result = await tool(command="cat hello.txt")
            self.assertEqual(result["return_code"], 0)
            self.assertEqual(result["stdout"].strip(), "ok")


class ToolExecutorGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_deny_short_circuits_without_calling_tool(self):
        called = {"value": False}

        async def lookup():
            called["value"] = True
            return "should not run"

        class DenyGuard:
            async def pre_execute(self, ctx):
                return ToolGuardResult.deny("blocked by policy")

            async def post_execute(self, ctx, result):
                return result

        executor = ToolExecutor(
            tool_manager=FakeToolManager(tools={"lookup": lookup}),
            message_storage=InMemoryMessageStorage(),
            client=None,
            guards=[DenyGuard()],
        )
        tool_message, display_result = await executor.execute_single(FakeToolCall(name="lookup"))
        self.assertFalse(called["value"])
        self.assertIsNone(display_result)
        self.assertEqual(tool_message["content"], "Tool error: blocked by policy")

    async def test_ask_without_approver_denies(self):
        async def lookup():
            return "ran"

        class AskGuard:
            async def pre_execute(self, ctx):
                return ToolGuardResult.ask("destructive shell")

            async def post_execute(self, ctx, result):
                return result

        executor = ToolExecutor(
            tool_manager=FakeToolManager(tools={"lookup": lookup}),
            message_storage=InMemoryMessageStorage(),
            client=None,
            guards=[AskGuard()],
        )
        tool_message, _ = await executor.execute_single(FakeToolCall(name="lookup"))
        self.assertEqual(tool_message["content"], "Tool error: approval required: destructive shell")

    async def test_shell_guard_blocks_executor_before_tool_body(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            called = {"value": False}

            async def run_command(command: str, working_directory=None, timeout=30):
                called["value"] = True
                return {"stdout": "ran", "stderr": "", "return_code": 0}

            executor = ToolExecutor(
                tool_manager=FakeToolManager(tools={"run_command": run_command}),
                message_storage=InMemoryMessageStorage(),
                client=None,
                guards=[WorkspaceShellGuard(tmpdir)],
                workspace_dir=Path(tmpdir),
            )
            tool_message, _ = await executor.execute_single(
                FakeToolCall(
                    name="run_command",
                    arguments='{"command": "pwd", "working_directory": "/tmp"}',
                )
            )
            self.assertFalse(called["value"])
            self.assertIn("Tool error:", tool_message["content"])
            self.assertIn("outside the agent workspace", tool_message["content"])

    async def test_pre_execute_receives_turn_attribution(self):
        seen = {}

        async def lookup():
            return "ok"

        class CaptureGuard:
            async def pre_execute(self, ctx):
                seen["user_id"] = ctx.user_id
                seen["channel"] = ctx.channel
                seen["room_name"] = ctx.room_name
                seen["inbox_kind"] = ctx.inbox_kind
                return ToolGuardResult.allow()

            async def post_execute(self, ctx, result):
                return result

        executor = ToolExecutor(
            tool_manager=FakeToolManager(tools={"lookup": lookup}),
            message_storage=InMemoryMessageStorage(),
            client=None,
            guards=[CaptureGuard()],
        )
        await executor.handle_tool_calls(
            [FakeToolCall(name="lookup")],
            [],
            user_id="alice",
            channel="feishu",
            room_name="Eng",
            inbox_kind="user_turn",
        )
        self.assertEqual(seen["user_id"], "alice")
        self.assertEqual(seen["channel"], "feishu")
        self.assertEqual(seen["room_name"], "Eng")
        self.assertEqual(seen["inbox_kind"], "user_turn")
