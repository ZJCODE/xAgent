"""The only process-lifecycle boundary for one xAgent Runtime."""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ...settings import XAgentSettings
from .client import RuntimeClient, RuntimeUnavailable


LaunchState = Literal[
    "started",
    "already_running",
    "stopped",
    "already_stopped",
    "restarted",
]


class RuntimeLaunchError(RuntimeError):
    """Raised when the Runtime cannot reach the requested lifecycle state."""


@dataclass(frozen=True)
class RuntimeLaunchOutcome:
    state: LaunchState
    pid: int | None
    instance_id: str = ""
    log_path: Path | None = None

    @property
    def changed(self) -> bool:
        return self.state in {"started", "stopped", "restarted"}


class RuntimeLauncher:
    """Validate, launch and control the single Runtime for one Agent directory."""

    def __init__(self, config_dir: str | Path) -> None:
        self.config_dir = Path(config_dir).expanduser().resolve()
        self.run_dir = self.config_dir / "run"
        self.log_path = self.run_dir / "runtime.log"
        self.client = RuntimeClient(self.config_dir)

    def status(
        self,
        *,
        timeout_seconds: float = 2.0,
    ) -> dict[str, Any] | None:
        try:
            return self.client.status(timeout_seconds=timeout_seconds)
        except RuntimeUnavailable:
            return None
        except RuntimeError as exc:
            raise RuntimeLaunchError(f"Runtime status failed: {exc}") from exc

    def start(self, *, timeout_seconds: float = 10.0) -> RuntimeLaunchOutcome:
        current = self.status(timeout_seconds=0.5)
        if current is not None:
            return self._outcome("already_running", current)
        self._validate_agent_directory()

        try:
            self._prepare_run_directory()
            process = self._spawn()
        except OSError as exc:
            raise RuntimeLaunchError(f"Cannot spawn Runtime: {exc}") from exc
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))

        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            current = self.status(timeout_seconds=min(0.25, remaining))
            if current is not None:
                if int(current["pid"]) == process.pid:
                    return self._outcome("started", current)
                self._terminate(process)
                return self._outcome("already_running", current)

            return_code = process.poll()
            if return_code is not None:
                raise RuntimeLaunchError(
                    f"Runtime exited during startup with code {return_code}. "
                    f"See {self.log_path}."
                )
            time.sleep(0.05)

        self._terminate(process)
        raise RuntimeLaunchError(
            f"Runtime did not become ready within {timeout_seconds:g} seconds. "
            f"See {self.log_path}."
        )

    def stop(self, *, timeout_seconds: float = 15.0) -> RuntimeLaunchOutcome:
        current = self.status()
        if current is None:
            return RuntimeLaunchOutcome(
                state="already_stopped",
                pid=None,
                log_path=self.log_path,
            )

        pid = int(current["pid"])
        instance_id = str(current.get("instance_id") or "")
        try:
            self.client.request("POST", "/v1/runtime/stop", timeout=5.0)
        except (RuntimeUnavailable, RuntimeError) as exc:
            raise RuntimeLaunchError(
                "Runtime became unreachable before acknowledging shutdown."
            ) from exc

        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            observed = self.status(timeout_seconds=min(0.25, remaining))
            if observed is None:
                return RuntimeLaunchOutcome(
                    state="stopped",
                    pid=pid,
                    instance_id=instance_id,
                    log_path=self.log_path,
                )
            if str(observed.get("instance_id") or "") != instance_id:
                raise RuntimeLaunchError(
                    "A new Runtime instance replaced the one being stopped."
                )
            time.sleep(0.05)
        raise RuntimeLaunchError(
            f"Runtime pid {pid} did not stop within {timeout_seconds:g} seconds."
        )

    def restart(
        self,
        *,
        stop_timeout_seconds: float = 15.0,
        start_timeout_seconds: float = 10.0,
    ) -> RuntimeLaunchOutcome:
        self.stop(timeout_seconds=stop_timeout_seconds)
        started = self.start(timeout_seconds=start_timeout_seconds)
        return RuntimeLaunchOutcome(
            state="restarted",
            pid=started.pid,
            instance_id=started.instance_id,
            log_path=started.log_path,
        )

    async def run_foreground(self) -> None:
        self._validate_agent_directory()
        from .application import run_runtime

        await run_runtime(self.config_dir)

    def _validate_agent_directory(self) -> None:
        if not self.config_dir.is_dir():
            raise RuntimeLaunchError(
                f"Agent directory does not exist: {self.config_dir}"
            )
        try:
            XAgentSettings.load(self.config_dir / "config.yaml")
        except Exception as exc:
            raise RuntimeLaunchError(f"Invalid Agent configuration: {exc}") from exc
        identity_path = self.config_dir / "identity.md"
        try:
            identity = identity_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeLaunchError(f"Cannot read Agent identity: {identity_path}") from exc
        if not identity:
            raise RuntimeLaunchError(f"Agent identity is empty: {identity_path}")

    def _prepare_run_directory(self) -> None:
        if self.run_dir.is_symlink():
            raise OSError(f"Runtime directory must not be a symbolic link: {self.run_dir}")
        if self.run_dir.exists() and not self.run_dir.is_dir():
            raise OSError(f"Runtime path is not a directory: {self.run_dir}")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.run_dir.chmod(0o700)

    def _spawn(self) -> subprocess.Popen[bytes]:
        command = [
            sys.executable,
            "-m",
            "xagent.core.runtime.worker",
            "--config-dir",
            str(self.config_dir),
        ]
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.log_path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError(f"Runtime log is not a regular file: {self.log_path}")
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
        except Exception:
            os.close(descriptor)
            raise
        with os.fdopen(descriptor, "ab", buffering=0) as log:
            return subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3.0)

    def _outcome(
        self,
        state: LaunchState,
        status: dict[str, Any],
    ) -> RuntimeLaunchOutcome:
        return RuntimeLaunchOutcome(
            state=state,
            pid=int(status["pid"]),
            instance_id=str(status.get("instance_id") or ""),
            log_path=self.log_path,
        )
