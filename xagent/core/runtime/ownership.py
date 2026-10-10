"""Local runtime ownership, independent of channels and process PID files."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

_owned_roots: dict[Path, int] = {}


class RuntimeAlreadyRunning(RuntimeError):
    """Another process owns this agent directory."""


def canonical_root(root: str | Path) -> Path:
    return Path(root).expanduser().resolve()


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    lock_path: Path
    socket_path: Path
    state_path: Path
    pid_path: Path
    log_path: Path


def runtime_paths(root: str | Path) -> RuntimePaths:
    resolved = canonical_root(root)
    digest = hashlib.sha256(os.fsencode(resolved)).hexdigest()[:24]
    uid = os.getuid() if hasattr(os, "getuid") else "local"
    return RuntimePaths(
        root=resolved,
        lock_path=resolved / ".runtime" / "owner.lock",
        socket_path=Path("/tmp") / f"xagent-{uid}" / f"{digest}.sock",
        state_path=resolved / ".runtime" / "status.json",
        pid_path=resolved / "run" / "runtime.pid",
        log_path=resolved / "logs" / "runtime.log",
    )


class RuntimeOwnership:
    """Hold the OS lock for the whole lifetime of a live or offline writer."""

    def __init__(self, root: str | Path):
        self.paths = runtime_paths(root)
        self._handle = None

    def acquire(self) -> "RuntimeOwnership":
        import fcntl

        if self._handle is not None:
            return self
        self.paths.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.paths.lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise RuntimeAlreadyRunning(f"Agent is already running: {self.paths.root}") from exc
        except BaseException:
            handle.close()
            raise
        self._handle = handle
        _owned_roots[self.paths.root] = os.getpid()
        return self

    def release(self) -> None:
        import fcntl

        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
                _owned_roots.pop(self.paths.root, None)

    def __enter__(self) -> "RuntimeOwnership":
        return self.acquire()

    def __exit__(self, *_args) -> None:
        self.release()


def runtime_is_active(root: str | Path) -> bool:
    """Probe the authoritative lock; never create directories or lock files."""
    import fcntl

    path = runtime_paths(root).lock_path
    try:
        handle = path.open("r", encoding="utf-8")
    except FileNotFoundError:
        return False
    with handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False


def runtime_owned_here(root: str | Path) -> bool:
    return _owned_roots.get(canonical_root(root)) == os.getpid()


def prepare_socket_directory(paths: RuntimePaths) -> None:
    directory = paths.socket_path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid():
        raise RuntimeError(f"Unsafe runtime socket directory: {directory}")
    directory.chmod(0o700)


def write_runtime_status(paths: RuntimePaths, status: dict) -> None:
    paths.state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = paths.state_path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(status, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(paths.state_path)
