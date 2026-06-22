"""Small process-supervision helpers for CLI-managed channels."""
from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence


DEFAULT_STARTUP_TIMEOUT = 2.0
DEFAULT_STOP_TIMEOUT = 5.0
STOP_POLL_INTERVAL = 0.1


@dataclass(frozen=True)
class ManagedProcessPaths:
    """Filesystem paths used for one managed channel process."""

    pid_path: Path
    log_path: Path


@dataclass(frozen=True)
class StartResult:
    """Result of starting a managed process."""

    ok: bool
    pid: Optional[int] = None
    return_code: Optional[int] = None
    error: str = ""
    recent_output: str = ""


def managed_paths(config_dir: Path, channel: str) -> ManagedProcessPaths:
    """Return the PID and log paths for a public channel name."""
    return ManagedProcessPaths(
        pid_path=config_dir / "run" / f"{channel}.pid",
        log_path=config_dir / "logs" / f"{channel}.log",
    )


def read_pid(pid_path: Path) -> Optional[int]:
    if not pid_path.is_file():
        return None
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def write_pid(pid_path: Path, pid: int) -> None:
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(f"{pid}\n", encoding="utf-8")


def remove_pid(pid_path: Path, expected_pid: Optional[int] = None) -> None:
    if not pid_path.exists():
        return
    if expected_pid is not None:
        current_pid = read_pid(pid_path)
        if current_pid is not None and current_pid != expected_pid:
            return
    try:
        pid_path.unlink()
    except OSError:
        pass


def pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if _pid_looks_defunct(pid):
        return False
    return True


def _pid_looks_defunct(pid: int) -> bool:
    if os.name != "posix":
        return False
    try:
        result = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    if result.returncode != 0:
        return False
    state = "".join((result.stdout or "").split())
    return state.startswith("Z")


def running_pid(pid_path: Path) -> Optional[int]:
    pid = read_pid(pid_path)
    if pid is None:
        remove_pid(pid_path)
        return None
    if pid_is_running(pid):
        return pid
    remove_pid(pid_path, expected_pid=pid)
    return None


def tail_text(path: Path, max_lines: int = 20) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-max_lines:]).strip()


def wait_for_process_exit(pid: int, timeout: float = DEFAULT_STOP_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_is_running(pid):
            return True
        time.sleep(STOP_POLL_INTERVAL)
    return not pid_is_running(pid)


def start_background(
    command: Sequence[str],
    *,
    pid_path: Path,
    log_path: Path,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
) -> StartResult:
    """Start a detached process, write its PID, and verify it survives startup."""
    existing_pid = running_pid(pid_path)
    if existing_pid is not None:
        return StartResult(ok=False, pid=existing_pid, error=f"already running (pid={existing_pid})")

    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    with log_path.open("ab") as log_handle:
        try:
            process = subprocess.Popen(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
                env=env,
            )
        except OSError as exc:
            return StartResult(ok=False, error=str(exc))

    write_pid(pid_path, process.pid)

    try:
        process.wait(timeout=startup_timeout)
    except subprocess.TimeoutExpired:
        return StartResult(ok=True, pid=process.pid)

    remove_pid(pid_path, expected_pid=process.pid)
    return StartResult(
        ok=False,
        pid=process.pid,
        return_code=process.returncode,
        error="exited during startup",
        recent_output=tail_text(log_path),
    )


def stop_managed_process(pid_path: Path, timeout: float = DEFAULT_STOP_TIMEOUT) -> tuple[bool, str]:
    """Stop the process in pid_path. Returns (ok, message)."""
    pid = running_pid(pid_path)
    if pid is None:
        return True, "not running"

    stop_signal = getattr(signal, "SIGTERM", signal.SIGINT)
    try:
        os.kill(pid, stop_signal)
    except ProcessLookupError:
        remove_pid(pid_path, expected_pid=pid)
        return True, f"already stopped (pid={pid})"
    except PermissionError as exc:
        return False, f"failed to stop pid {pid}: {exc}"

    if wait_for_process_exit(pid, timeout):
        remove_pid(pid_path, expected_pid=pid)
        return True, f"stopped (pid={pid})"

    kill_signal = getattr(signal, "SIGKILL", None)
    if kill_signal is None:
        return False, f"timed out stopping pid {pid}"

    try:
        os.kill(pid, kill_signal)
    except ProcessLookupError:
        remove_pid(pid_path, expected_pid=pid)
        return True, f"stopped (pid={pid})"
    except PermissionError as exc:
        return False, f"failed to force-stop pid {pid}: {exc}"

    if wait_for_process_exit(pid, timeout):
        remove_pid(pid_path, expected_pid=pid)
        return True, f"force-stopped (pid={pid})"
    return False, f"timed out force-stopping pid {pid}"
