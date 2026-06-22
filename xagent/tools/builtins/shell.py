import asyncio
import functools
import logging
from typing import Optional

from xagent.config.schema import AgentConfig
from xagent.tools.protocol import function_tool

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
        "Prefer read-only inspection first; get explicit approval for destructive or sensitive operations. "
        "Output is capped and truncated."
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
        )

    workspace_run_command.tool_spec = run_command.tool_spec
    workspace_run_command.__name__ = run_command.__name__
    return workspace_run_command


async def _run_shell_command(
    command: str,
    working_directory: Optional[str] = None,
    timeout: int = 30,
) -> dict:
    """Execute a shell command and return stdout, stderr, and return code."""
    if not command or not command.strip():
        return {"stdout": "", "stderr": "Empty command", "return_code": -1}

    timeout = max(1, min(timeout, AgentConfig.MAX_COMMAND_TIMEOUT))
    max_output = AgentConfig.MAX_COMMAND_OUTPUT_SIZE

    logger.warning(
        "[SHELL AUDIT] Executing command: %s | cwd: %s | timeout: %ds",
        command, working_directory or "(inherit)", timeout,
    )

    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_directory,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )

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

    except asyncio.TimeoutError:
        # Kill the timed-out process
        try:
            process.kill()  # type: ignore[possibly-undefined]
            await process.wait()  # type: ignore[possibly-undefined]
        except Exception:
            pass
        return {
            "stdout": "",
            "stderr": f"Command timed out after {timeout} seconds",
            "return_code": -1,
        }
    except Exception as e:
        logger.error("Command execution error: %s", e)
        return {
            "stdout": "",
            "stderr": str(e),
            "return_code": -1,
        }
