import asyncio
import copy
import functools
import logging
import re
from typing import Any, Callable, Dict, Optional

from xagent.core.config import AgentConfig
from xagent.core.tooling.guards import WorkspaceEscapeError, resolve_workspace_cwd
from xagent.utils.tool_decorator import function_tool

logger = logging.getLogger(__name__)

JobStarter = Callable[..., Any]

# Long-wait heuristics (P0 UX): keep the formal turn lock free when async jobs exist.
_SLEEP_SECONDS_RE = re.compile(
    r"(?:^|[;&|\n]|&&|\|\|)\s*(?:sudo\s+)?sleep\s+(?P<value>\d+(?:\.\d+)?)(?P<unit>[smhd]?)",
    re.IGNORECASE,
)
_POLL_LOOP_RE = re.compile(
    r"(?:\bwhile\b|\bfor\s+\w+\s+in\b)",
    re.IGNORECASE,
)
_WAITISH_RE = re.compile(
    r"(?:\bsleep\b|\bwatch\b|\btail\s+-f\b|\bmonitor\b|\buntil\b|"
    r"\bping\s+-c\b|\bapt(?:-get)?\b|\bdocker\s+build\b|\bnpm\s+install\b|"
    r"\bpip(?:3)?\s+install\b|\bwget\b|\bcurl\b.*(?:-O|--output)|\bmake\b)",
    re.IGNORECASE,
)
_LONG_SLEEP_SECONDS = 5.0
_LONG_TIMEOUT_SECONDS = 60


def _sleep_seconds(value: str, unit: str) -> float:
    amount = float(value)
    unit = (unit or "s").lower()
    if unit == "m":
        return amount * 60.0
    if unit == "h":
        return amount * 3600.0
    if unit == "d":
        return amount * 86400.0
    return amount


def looks_like_long_blocking_command(command: str, timeout: int = 30) -> bool:
    """True when a foreground run would likely pin the turn lock for a long time."""
    text = (command or "").strip()
    if not text:
        return False
    timeout_s = max(1, int(timeout or 30))
    for match in _SLEEP_SECONDS_RE.finditer(text):
        if _sleep_seconds(match.group("value"), match.group("unit") or "s") >= _LONG_SLEEP_SECONDS:
            return True
    if _POLL_LOOP_RE.search(text) and re.search(r"\bsleep\b", text, re.IGNORECASE):
        return True
    if timeout_s >= _LONG_TIMEOUT_SECONDS and _WAITISH_RE.search(text):
        return True
    return False


def _refuse_foreground_long_wait(command: str, timeout: int) -> dict:
    return {
        "stdout": "",
        "stderr": (
            "Refusing foreground long-wait command while conversation should stay responsive. "
            "Re-call run_command with background=true (requires runtime.async_jobs.enabled), "
            "then end the turn and wait for the JOB_RESULT turn. "
            f"Detected long-wait pattern in: {command!r} (timeout={timeout})."
        ),
        "return_code": -1,
        "background": False,
        "status": "refused_foreground_wait",
        "command": command,
        "hint": "use background=true; do not foreground sleep/poll after a Started job",
    }


ContextProvider = Callable[[], Dict[str, Any]]


def _truncate(text: str, max_size: int) -> str:
    """Truncate text to max_size bytes, append marker if truncated."""
    if len(text.encode("utf-8", errors="replace")) <= max_size:
        return text
    truncated = text.encode("utf-8", errors="replace")[:max_size].decode(
        "utf-8", errors="ignore"
    )
    return truncated + "\n[truncated]"


_RUN_COMMAND_DESCRIPTION = (
    "Run a scoped shell command and return stdout, stderr, and exit code. "
    "Default cwd is the agent workspace; routine reads and edits there are fine. "
    "Prefer read-only inspection first. Get explicit approval before destructive "
    "operations (large deletes, irreversible overwrites), sensitive work, or any "
    "mutation outside the workspace. Never expose secrets. Output is capped and truncated. "
    "For long-blocking work (sleep, downloads, builds, apt, docker build, long monitors), "
    "set background=true so the main turn can keep talking; you will get a job_id and "
    "a later turn reports completion. When runtime.async_jobs.enabled, obvious long waits "
    "(sleep>=5s, poll loops, or timeout>=60 on wait-ish commands) auto-upgrade to background. "
    "After status=started, do NOT foreground-wait or poll that same job; end the turn and "
    "let a later JOB_RESULT turn report completion."
)

_RUN_COMMAND_PARAM_DESCRIPTIONS = {
    "command": (
        "Shell command to execute. Keep it specific and bounded."
    ),
    "working_directory": "Optional cwd; defaults to the agent workspace in standard runtimes.",
    "timeout": "Maximum seconds, 1-300. Defaults to 30. Applies to foreground runs and to the background Worker.",
    "background": (
        "When true, start the command as an async Worker and return immediately with "
        "job_id/status=started. Use for long sleeps, downloads, builds, monitors, and other "
        "work that would block the conversation. Requires runtime.async_jobs.enabled. "
        "After started, end the turn; do not call run_command again just to sleep/poll "
        "until the same job finishes."
    ),
}


@function_tool(
    name="run_command",
    description=_RUN_COMMAND_DESCRIPTION,
    param_descriptions=_RUN_COMMAND_PARAM_DESCRIPTIONS,
)
async def run_command(
    command: str,
    working_directory: Optional[str] = None,
    timeout: int = 30,
    background: bool = False,
) -> dict:
    """Execute a shell command and return stdout, stderr, and return code."""
    if background:
        return {
            "stdout": "",
            "stderr": (
                "background=true requires runtime.async_jobs.enabled and a bound "
                "Router job starter; refusing to run synchronously so miswiring is visible."
            ),
            "return_code": -1,
            "background": True,
            "status": "unavailable",
        }
    return await _run_shell_command(
        command=command,
        working_directory=working_directory,
        timeout=timeout,
    )


def create_workspace_run_command_tool(
    default_working_directory: str,
    *,
    job_starter: Optional[JobStarter] = None,
    async_jobs_enabled: bool = False,
    max_active: Optional[int] = None,
    context_provider: Optional[ContextProvider] = None,
    active_count_provider: Optional[Callable[[], int]] = None,
):
    """Create a run_command tool whose default cwd is the agent workspace."""

    @functools.wraps(run_command)
    async def workspace_run_command(
        command: str,
        working_directory: Optional[str] = None,
        timeout: int = 30,
        background: bool = False,
    ) -> dict:
        effective_background = bool(background)
        auto_upgraded = False
        if (
            not effective_background
            and async_jobs_enabled
            and looks_like_long_blocking_command(command, timeout)
        ):
            if job_starter is not None:
                effective_background = True
                auto_upgraded = True
                logger.warning(
                    "[SHELL AUDIT] Auto-upgrading long-wait run_command to background: %s",
                    command,
                )
            else:
                return _refuse_foreground_long_wait(command, timeout)

        if effective_background:
            result = await _start_background_shell(
                command=command,
                working_directory=working_directory or default_working_directory,
                timeout=timeout,
                workspace_root=default_working_directory,
                job_starter=job_starter,
                async_jobs_enabled=async_jobs_enabled,
                max_active=max_active,
                context_provider=context_provider,
                active_count_provider=active_count_provider,
            )
            if auto_upgraded and isinstance(result, dict):
                result = {
                    **result,
                    "auto_background": True,
                    "guidance": (
                        "Long-wait command was moved to a background Worker. "
                        "End this turn; a later JOB_RESULT turn will report completion. "
                        "Do not foreground sleep/poll waiting for this job."
                    ),
                }
            return result
        return await _run_shell_command(
            command=command,
            working_directory=working_directory or default_working_directory,
            timeout=timeout,
            workspace_root=default_working_directory,
        )

    tool_spec = copy.deepcopy(run_command.tool_spec)
    workspace_run_command.tool_spec = tool_spec
    workspace_run_command.__name__ = run_command.__name__
    return workspace_run_command


async def _start_background_shell(
    *,
    command: str,
    working_directory: Optional[str],
    timeout: int,
    workspace_root: Optional[str],
    job_starter: Optional[JobStarter],
    async_jobs_enabled: bool,
    max_active: Optional[int],
    context_provider: Optional[ContextProvider],
    active_count_provider: Optional[Callable[[], int]],
) -> dict:
    if not async_jobs_enabled or job_starter is None:
        return {
            "stdout": "",
            "stderr": (
                "background=true requires runtime.async_jobs.enabled and a bound "
                "Router job starter; refusing to run synchronously so miswiring is visible."
            ),
            "return_code": -1,
            "background": True,
            "status": "unavailable",
            "command": command,
        }
    if max_active is not None and active_count_provider is not None:
        try:
            active = int(active_count_provider())
        except Exception:
            active = 0
        if active >= int(max_active):
            return {
                "stdout": "",
                "stderr": f"async job limit reached ({max_active}); try again when a Worker finishes",
                "return_code": -1,
                "background": True,
                "status": "rejected",
                "command": command,
            }

    context = {}
    if context_provider is not None:
        try:
            context = dict(context_provider() or {})
        except Exception as exc:
            logger.warning("async job context_provider failed: %s", exc)
            context = {}

    async def work() -> dict:
        return await _run_shell_command(
            command=command,
            working_directory=working_directory,
            timeout=timeout,
            workspace_root=workspace_root,
        )

    try:
        handle = job_starter(
            "run_command",
            work,
            channel=context.get("channel"),
            actor_id=context.get("actor_id"),
            thread_id=context.get("thread_id"),
            metadata={
                "command": command,
                "cwd": working_directory,
                "timeout": timeout,
                "tool": "run_command",
            },
        )
    except Exception as exc:
        logger.error("Failed to start background run_command: %s", exc)
        return {
            "stdout": "",
            "stderr": f"failed to start background job: {exc}",
            "return_code": -1,
            "background": True,
            "status": "failed",
            "command": command,
        }

    payload = handle.to_started_payload() if hasattr(handle, "to_started_payload") else {
        "job_id": getattr(handle, "job_id", ""),
        "status": getattr(getattr(handle, "status", None), "value", "started"),
    }
    return {
        **payload,
        "background": True,
        "command": command,
        "status": "started",
    }


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
