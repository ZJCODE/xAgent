import asyncio
import functools
import logging
import os
import signal
from typing import Optional

from xagent.core.config import AgentConfig
from xagent.core.turn import wait_first
from xagent.core.tooling.guards import WorkspaceEscapeError, resolve_workspace_cwd
from xagent.utils.tool_decorator import function_tool

logger = logging.getLogger(__name__)


def _truncate(text: str, max_size: int) -> str:
    """Truncate text to max_size bytes, append marker if truncated."""
    if len(text.encode("utf-8", errors="replace")) <= max_size:
        return text
    truncated = text.encode("utf-8", errors="replace")[:max_size].decode(
        "utf-8", errors="ignore"
    )
    return truncated + "\n[truncated]"


@function_tool(
    name="run_command",
    description=(
        "Run a scoped shell command and return stdout, stderr, and exit code. "
        "Default cwd is the agent workspace; routine reads and edits there are fine. "
        "Prefer read-only inspection first. Get explicit approval before destructive "
        "operations (large deletes, irreversible overwrites), sensitive work, or any "
        "mutation outside the workspace. Never expose secrets. Output is capped and truncated."
    ),
    param_descriptions={
        "command": (
            "Shell command to execute. Keep it specific and bounded."
        ),
        "working_directory": "Optional cwd; defaults to the agent workspace in standard runtimes.",
        "timeout": "Maximum seconds, 1-300. Defaults to 30.",
    },
)
async def run_command(
    command: str,
    working_directory: Optional[str] = None,
    timeout: int = 30,
) -> dict:
    """Execute a shell command and return stdout, stderr, and return code."""
    return await _run_shell_command(
        command=command,
        working_directory=working_directory,
        timeout=timeout,
    )


def create_workspace_run_command_tool(default_working_directory: str):
    """Create a run_command tool whose default cwd is the agent workspace."""

    @functools.wraps(run_command)
    async def workspace_run_command(
        command: str,
        working_directory: Optional[str] = None,
        timeout: int = 30,
    ) -> dict:
        return await _run_shell_command(
            command=command,
            working_directory=working_directory or default_working_directory,
            timeout=timeout,
            workspace_root=default_working_directory,
        )

    workspace_run_command.tool_spec = run_command.tool_spec
    workspace_run_command.__name__ = run_command.__name__
    return workspace_run_command


async def _run_shell_command(
    command: str,
    working_directory: Optional[str] = None,
    timeout: int = 30,
    workspace_root: Optional[str] = None,
) -> dict:
    """Execute a shell command and return stdout, stderr, and return code."""
    if not command or not command.strip():
        return {"stdout": "", "stderr": "Empty command", "return_code": -1}

    timeout = max(1, min(timeout, AgentConfig.MAX_COMMAND_TIMEOUT))
    max_output = AgentConfig.MAX_COMMAND_OUTPUT_SIZE
    cwd = working_directory
    if workspace_root:
        try:
            cwd = resolve_workspace_cwd(working_directory, workspace_root)
        except WorkspaceEscapeError as exc:
            return {
                "stdout": "",
                "stderr": str(exc),
                "return_code": -1,
            }

    logger.warning(
        "[SHELL AUDIT] Executing command: %s | cwd: %s | timeout: %ds",
        command, cwd or "(inherit)", timeout,
    )

    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            start_new_session=True,
        )
        communicate_task = asyncio.create_task(process.communicate())
        status = await wait_first(communicate_task, timeout=timeout)
        if status == "ok":
            stdout_bytes, stderr_bytes = communicate_task.result()
            outcome = "ok"
        else:
            outcome = status
            _kill_shell_process(process)
            try:
                await communicate_task
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            stdout_bytes, stderr_bytes = b"", b""

        if outcome == "timeout":
            return {
                "stdout": "",
                "stderr": (
                    f"Command timed out after {timeout}s. "
                    f"Retry with higher timeout (max {AgentConfig.MAX_COMMAND_TIMEOUT}) "
                    "if still progressing; otherwise split into smaller commands."
                ),
                "return_code": -1,
            }
        if outcome == "aborted":
            return {
                "stdout": "",
                "stderr": "Command aborted.",
                "return_code": -1,
            }

        stdout = _truncate(stdout_bytes.decode("utf-8", errors="replace"), max_output)
        stderr = _truncate(stderr_bytes.decode("utf-8", errors="replace"), max_output)

        logger.warning(
            "[SHELL AUDIT] Command finished: return_code=%s | stdout_len=%d | stderr_len=%d",
            process.returncode, len(stdout), len(stderr),
        )

        return {
            "stdout": stdout,
            "stderr": stderr,
            "return_code": process.returncode,
        }

    except Exception as e:
        logger.error("Command execution error: %s", e)
        return {
            "stdout": "",
            "stderr": str(e),
            "return_code": -1,
        }


def _kill_shell_process(process: asyncio.subprocess.Process) -> None:
    """Kill the shell and any children in its session."""
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except ProcessLookupError:
            pass
