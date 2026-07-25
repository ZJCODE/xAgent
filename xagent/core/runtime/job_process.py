"""POSIX process identity and termination helpers for background jobs."""
from __future__ import annotations

import contextlib
import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional


_BOOT_SESSION_ID: Optional[str] = None


def boot_session_id() -> str:
    """Return a stable identifier for the current operating-system boot."""
    global _BOOT_SESSION_ID
    if _BOOT_SESSION_ID is not None:
        return _BOOT_SESSION_ID
    linux_boot_id = Path("/proc/sys/kernel/random/boot_id")
    try:
        value = linux_boot_id.read_text(encoding="utf-8").strip()
        if value:
            _BOOT_SESSION_ID = f"linux:{value}"
            return _BOOT_SESSION_ID
    except OSError:
        pass

    if sys.platform == "darwin":
        try:
            class Timeval(ctypes.Structure):
                _fields_ = [
                    ("tv_sec", ctypes.c_long),
                    ("tv_usec", ctypes.c_int),
                ]

            value = Timeval()
            size = ctypes.c_size_t(ctypes.sizeof(value))
            libc = ctypes.CDLL(None, use_errno=True)
            result = libc.sysctlbyname(
                b"kern.boottime",
                ctypes.byref(value),
                ctypes.byref(size),
                None,
                0,
            )
            if result == 0 and value.tv_sec > 0:
                _BOOT_SESSION_ID = f"darwin:{value.tv_sec}:{value.tv_usec}"
                return _BOOT_SESSION_ID
        except (AttributeError, OSError):
            pass
        try:
            completed = subprocess.run(
                ["sysctl", "-n", "kern.boottime"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
            value = completed.stdout.strip()
            if value:
                _BOOT_SESSION_ID = f"darwin:{value}"
                return _BOOT_SESSION_ID
        except (OSError, subprocess.SubprocessError):
            pass

    if sys.platform == "darwin":
        # Sandboxed macOS processes may be denied kern.boottime even though
        # libproc identity lookup is available. A minute-rounded boot epoch,
        # derived from the system-wide monotonic clock, is stable across local
        # processes while still changing after reboot. Any clock anomaly only
        # makes verification fail closed.
        approximate_boot_epoch = time.time() - time.monotonic()
        _BOOT_SESSION_ID = f"darwin-approx:{int(approximate_boot_epoch // 60)}"
        return _BOOT_SESSION_ID

    # The fallback is deliberately scoped to this Python boot session. It can
    # cause a conservative "interrupted" result, but never a wrong process kill.
    _BOOT_SESSION_ID = f"fallback:{os.getpid()}:{time.time_ns()}"
    return _BOOT_SESSION_ID


def process_start_identity(pid: int) -> Optional[str]:
    """Return a platform-specific process start fingerprint."""
    if pid <= 0:
        return None

    linux_stat = Path(f"/proc/{pid}/stat")
    try:
        raw = linux_stat.read_text(encoding="utf-8")
        # Field 2 (comm) may contain spaces and parentheses. starttime is field
        # 22, which becomes index 19 after stripping pid and comm.
        tail = raw[raw.rfind(")") + 2 :].split()
        if len(tail) > 19:
            return f"linux-ticks:{tail[19]}"
    except OSError:
        pass

    if sys.platform == "darwin":
        try:
            class ProcBsdInfo(ctypes.Structure):
                _fields_ = [
                    ("pbi_flags", ctypes.c_uint32),
                    ("pbi_status", ctypes.c_uint32),
                    ("pbi_xstatus", ctypes.c_uint32),
                    ("pbi_pid", ctypes.c_uint32),
                    ("pbi_ppid", ctypes.c_uint32),
                    ("pbi_uid", ctypes.c_uint32),
                    ("pbi_gid", ctypes.c_uint32),
                    ("pbi_ruid", ctypes.c_uint32),
                    ("pbi_rgid", ctypes.c_uint32),
                    ("pbi_svuid", ctypes.c_uint32),
                    ("pbi_svgid", ctypes.c_uint32),
                    ("pbi_rfu_1", ctypes.c_uint32),
                    ("pbi_comm", ctypes.c_char * 16),
                    ("pbi_name", ctypes.c_char * 32),
                    ("pbi_nfiles", ctypes.c_uint32),
                    ("pbi_pgid", ctypes.c_uint32),
                    ("pbi_pjobc", ctypes.c_uint32),
                    ("e_tdev", ctypes.c_uint32),
                    ("e_tpgid", ctypes.c_uint32),
                    ("pbi_nice", ctypes.c_int32),
                    ("pbi_start_tvsec", ctypes.c_uint64),
                    ("pbi_start_tvusec", ctypes.c_uint64),
                ]

            info = ProcBsdInfo()
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            size = libproc.proc_pidinfo(
                int(pid),
                3,  # PROC_PIDTBSDINFO
                0,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if size == ctypes.sizeof(info) and info.pbi_start_tvsec:
                return f"darwin-start:{info.pbi_start_tvsec}:{info.pbi_start_tvusec}"
        except (AttributeError, OSError):
            pass
        try:
            completed = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
            value = completed.stdout.strip()
            if value:
                return f"darwin-lstart:{value}"
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def wait_for_process_start_identity(
    pid: int,
    *,
    timeout_seconds: float = 1.0,
) -> Optional[str]:
    """Wait briefly for platform process metadata to become observable."""
    deadline = time.monotonic() + max(0.05, timeout_seconds)
    while time.monotonic() < deadline:
        identity = process_start_identity(pid)
        if identity:
            return identity
        if not pid_is_running(pid):
            return None
        time.sleep(0.01)
    return process_start_identity(pid)


def pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    # kill(pid, 0) also succeeds for zombies. Treat those as stopped.
    if Path(f"/proc/{pid}/stat").is_file():
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            tail = raw[raw.rfind(")") + 2 :].split()
            if tail and tail[0] == "Z":
                return False
        except OSError:
            pass
    elif sys.platform == "darwin":
        try:
            completed = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
            if completed.stdout.strip().startswith("Z"):
                return False
        except (OSError, subprocess.SubprocessError):
            pass
    return True


def process_identity_matches(
    pid: int,
    *,
    expected_boot_id: str,
    expected_start_identity: str,
) -> bool:
    """Verify a PID still names the exact process that was recorded."""
    if not expected_boot_id or not expected_start_identity:
        return False
    current_boot_id = boot_session_id()
    if expected_boot_id != current_boot_id:
        # macOS sandboxing can hide kern.boottime, leaving us with an
        # approximate boot epoch that may shift after system sleep. libproc's
        # absolute process start timestamp remains sufficient to reject PID
        # reuse, including across reboot.
        if not (
            expected_boot_id.startswith("darwin-approx:")
            and current_boot_id.startswith("darwin-approx:")
            and expected_start_identity.startswith("darwin-start:")
        ):
            return False
    if not pid_is_running(pid):
        return False
    return process_start_identity(pid) == expected_start_identity


def signal_verified_process_group(
    pid: int,
    *,
    expected_boot_id: str,
    expected_start_identity: str,
    sig: signal.Signals,
) -> bool:
    """Signal a process group only after verifying its leader identity."""
    if not process_identity_matches(
        pid,
        expected_boot_id=expected_boot_id,
        expected_start_identity=expected_start_identity,
    ):
        return False
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def best_effort_kill_verified_group(
    pid: int,
    *,
    expected_boot_id: str,
    expected_start_identity: str,
) -> bool:
    """Kill an orphaned process group without ever trusting a bare PID."""
    return signal_verified_process_group(
        pid,
        expected_boot_id=expected_boot_id,
        expected_start_identity=expected_start_identity,
        sig=signal.SIGKILL,
    )


def safe_basic_environment() -> dict[str, str]:
    """Return a small environment that excludes provider/API secrets."""
    allowed = {
        "PATH",
        "HOME",
        "SHELL",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "USER",
        "LOGNAME",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


def close_quietly(handle) -> None:
    with contextlib.suppress(OSError):
        handle.close()
