import asyncio
import functools
import logging
import shlex
from pathlib import Path
from typing import Optional

from xagent.core.config import AgentConfig
from xagent.utils.tool_decorator import function_tool

logger = logging.getLogger(__name__)

_READ_ONLY_COMMANDS = {
    "du",
    "file",
    "find",
    "git",
    "grep",
    "head",
    "ls",
    "pwd",
    "rg",
    "sed",
    "stat",
    "tail",
    "wc",
}
_READ_ONLY_GIT_COMMANDS = {
    "describe",
    "diff",
    "grep",
    "log",
    "ls-files",
    "rev-parse",
    "shortlog",
    "show",
    "status",
}
_SHELL_SYNTAX = frozenset(";&|<>`$\n\r")
_MAX_COMMAND_CHARS = 4_096


@function_tool(
    name="run_command",
    description=(
        "Run one read-only command inside the agent workspace and return stdout, "
        "stderr, and exit code. Shell operators, writes, interpreters, and paths "
        "outside the workspace are rejected. Output is capped."
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
        workspace_root=working_directory,
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
            workspace_root=default_working_directory,
            timeout=timeout,
        )

    workspace_run_command.tool_spec = run_command.tool_spec
    workspace_run_command.__name__ = run_command.__name__
    return workspace_run_command


async def _run_shell_command(
    command: str,
    working_directory: Optional[str] = None,
    workspace_root: Optional[str] = None,
    timeout: int = 30,
) -> dict:
    """Execute one read-only process inside an explicit workspace boundary."""
    if not command or not command.strip():
        return {"stdout": "", "stderr": "Empty command", "return_code": -1}
    if len(command) > _MAX_COMMAND_CHARS:
        return {
            "stdout": "",
            "stderr": f"Command exceeds {_MAX_COMMAND_CHARS} characters",
            "return_code": -1,
        }

    try:
        root = Path(workspace_root or working_directory or Path.cwd()).expanduser().resolve()
        cwd = Path(working_directory or root).expanduser().resolve()
        cwd.relative_to(root)
        if not cwd.is_dir():
            raise ValueError(f"Working directory does not exist: {cwd}")
        arguments = _validated_arguments(command, root)
    except (OSError, ValueError) as exc:
        return {"stdout": "", "stderr": str(exc), "return_code": -1}

    timeout = max(1, min(timeout, AgentConfig.MAX_COMMAND_TIMEOUT))
    max_output = AgentConfig.MAX_COMMAND_OUTPUT_SIZE

    logger.warning(
        "[SHELL AUDIT] Executing command: %s | cwd: %s | timeout: %ds",
        command, cwd, timeout,
    )

    process = None
    stdout_task: asyncio.Task[str] | None = None
    stderr_task: asyncio.Task[str] | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
        )
        stdout_task = asyncio.create_task(
            _read_stream_limited(process.stdout, max_output)
        )
        stderr_task = asyncio.create_task(
            _read_stream_limited(process.stderr, max_output)
        )
        _, stdout, stderr = await asyncio.wait_for(
            asyncio.gather(process.wait(), stdout_task, stderr_task),
            timeout=timeout,
        )

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
        if process is not None:
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass
        for task in (stdout_task, stderr_task):
            if task is not None:
                task.cancel()
        return {
            "stdout": "",
            "stderr": f"Command timed out after {timeout} seconds",
            "return_code": -1,
        }
    except Exception as e:
        for task in (stdout_task, stderr_task):
            if task is not None:
                task.cancel()
        if process is not None and process.returncode is None:
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass
        logger.error("Command execution error: %s", e)
        return {
            "stdout": "",
            "stderr": str(e),
            "return_code": -1,
        }


async def _read_stream_limited(
    stream: asyncio.StreamReader | None,
    max_size: int,
) -> str:
    if stream is None:
        return ""
    chunks: list[bytes] = []
    stored = 0
    truncated = False
    while True:
        chunk = await stream.read(16_384)
        if not chunk:
            break
        remaining = max_size - stored
        if remaining > 0:
            kept = chunk[:remaining]
            chunks.append(kept)
            stored += len(kept)
        if len(chunk) > remaining:
            truncated = True
    text = b"".join(chunks).decode("utf-8", errors="replace")
    return text + ("\n[truncated]" if truncated else "")


def _validated_arguments(command: str, workspace_root: Path) -> list[str]:
    if any(character in command for character in _SHELL_SYNTAX):
        raise ValueError("Shell operators and substitutions are not allowed")
    try:
        arguments = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"Invalid command: {exc}") from exc
    if not arguments:
        raise ValueError("Empty command")

    executable = Path(arguments[0]).name
    if executable not in _READ_ONLY_COMMANDS:
        raise ValueError(f"Command is not in the read-only allowlist: {executable}")
    if executable == "git":
        subcommand = next(
            (value for value in arguments[1:] if not value.startswith("-")),
            "",
        )
        if subcommand not in _READ_ONLY_GIT_COMMANDS:
            raise ValueError(f"Git subcommand is not read-only: {subcommand or '(missing)'}")
    if executable == "find" and any(
        value in {
            "-delete",
            "-exec",
            "-execdir",
            "-fls",
            "-fprintf",
            "-fprint",
            "-fprint0",
            "-ok",
            "-okdir",
        }
        for value in arguments[1:]
    ):
        raise ValueError("Mutating find actions are not allowed")
    if executable == "sed" and any(
        value == "-i" or value.startswith("-i")
        for value in arguments[1:]
    ):
        raise ValueError("In-place editing is not allowed")
    if executable == "git" and any(
        value in {"-o", "--output", "--open-files-in-pager"}
        or value.startswith("--output=")
        or value.startswith("--open-files-in-pager=")
        for value in arguments[1:]
    ):
        raise ValueError("Git output files and external pagers are not allowed")
    if executable == "rg" and any(
        value == "--pre" or value.startswith("--pre=")
        for value in arguments[1:]
    ):
        raise ValueError("External preprocessors are not allowed")

    for value in arguments[1:]:
        if value == ".." or ".." in Path(value).parts:
            raise ValueError("Paths outside the workspace are not allowed")
        candidate_text = value.split("=", 1)[-1] if "=" in value else value
        candidate = Path(candidate_text).expanduser()
        if candidate.is_absolute():
            _require_inside_workspace(candidate, workspace_root)
        elif not value.startswith("-"):
            resolved = (workspace_root / candidate).resolve()
            if resolved.exists():
                _require_inside_workspace(resolved, workspace_root)
    return arguments


def _require_inside_workspace(path: Path, workspace_root: Path) -> None:
    try:
        path.resolve().relative_to(workspace_root)
    except ValueError as exc:
        raise ValueError("Paths outside the workspace are not allowed") from exc
