import asyncio
import logging
from typing import Optional

from xagent.core.config import AgentConfig
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
        "Execute a shell command and return its stdout, stderr, and return code. "
        "Use this for running CLI tools, inspecting files, executing scripts, or system operations. "
        "Safety guidelines: prefer read-only commands (ls, cat, grep, find, df, ps) for information gathering; "
        "for destructive or write operations (rm, mv, chmod, kill, etc.), confirm the user's intent first; "
        "never execute commands that could cause irreversible damage without explicit user approval. "
        "Output is capped at 50 KB per stream and auto-truncated."
    ),
    param_descriptions={
        "command": (
            "The shell command to execute. Prefer safe, scoped commands "
            "(e.g. 'ls -la /path', 'cat file.txt', 'git status'). "
            "Avoid broad destructive patterns like 'rm -rf /' or 'chmod -R 777 /'."
        ),
        "working_directory": "Optional working directory for the command. Defaults to the current process directory.",
        "timeout": "Maximum execution time in seconds (1-300). Defaults to 30.",
    },
)
async def run_command(
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
