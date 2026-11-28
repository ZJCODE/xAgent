import asyncio
import functools
import logging
import os
from typing import Dict, Optional

from xagent.core.config import AgentConfig
from xagent.core.tooling.guards import WorkspaceEscapeError, resolve_workspace_cwd
from xagent.utils.tool_decorator import function_tool

logger = logging.getLogger(__name__)

_SECRET_ENV_SUFFIXES = (
    "_API_KEY",
    "_SECRET_KEY",
    "_ACCESS_TOKEN",
    "_AUTH_TOKEN",
    "_BOT_TOKEN",
)
_SECRET_ENV_PREFIXES = (
    "LANGFUSE_",
    "WEIXIN_",
    "XAGENT_SECRET",
    "XAGENT_TOKEN",
    "XAGENT_API_KEY",
)


def _build_subprocess_env() -> Dict[str, str]:
    """Copy process env while stripping host provider and channel credentials."""
    sanitized: Dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper.endswith(_SECRET_ENV_SUFFIXES):
            continue
        if upper.startswith(_SECRET_ENV_PREFIXES) and any(
            token in upper for token in ("KEY", "SECRET", "TOKEN", "PASSWORD")
        ):
            continue
        sanitized[key] = value
    return sanitized


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
        "Run a shell command in your workspace and return stdout, stderr, and exit code "
        "(default cwd is your workspace; output is capped and truncated)."
    ),
    param_descriptions={
        "command": "Shell command to execute.",
        "working_directory": "Optional cwd inside your workspace.",
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
            env=_build_subprocess_env(),
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
            "stderr": (
                f"Command timed out after {timeout}s. "
                f"Retry with higher timeout (max {AgentConfig.MAX_COMMAND_TIMEOUT}) "
                "if still progressing; otherwise split into smaller commands."
            ),
            "return_code": -1,
        }
    except Exception as e:
        logger.error("Command execution error: %s", e)
        return {
            "stdout": "",
            "stderr": str(e),
            "return_code": -1,
        }
