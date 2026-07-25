"""On-demand per-agent dispatcher for background jobs."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO, Optional

from .job_process import (
    best_effort_kill_verified_group,
    boot_session_id,
    process_identity_matches,
    process_start_identity,
    wait_for_process_start_identity,
)
from .job_store import JobSettings, JobStore, now_ms


@dataclass(frozen=True)
class WorkerStartResult:
    available: bool
    state: str
    pid: Optional[int] = None
    warning: Optional[str] = None

    def to_view(self) -> dict:
        return {
            "available": self.available,
            "state": self.state,
            "pid": self.pid,
            "warning": self.warning,
        }


def _acquire_lock(path: Path, *, blocking: bool) -> Optional[IO[str]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(handle.fileno(), flags)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def ensure_worker_running(
    jobs_dir: Path | str,
    *,
    workspace_dir: Path | str | None = None,
    settings: Optional[JobSettings] = None,
) -> WorkerStartResult:
    """Start the per-agent worker exactly once and return immediately."""
    root = Path(jobs_dir).expanduser().resolve()
    workspace = (
        Path(workspace_dir).expanduser().resolve()
        if workspace_dir is not None
        else (root.parent / "workspace").resolve()
    )
    resolved_settings = settings or JobSettings()
    store = JobStore(root, workspace_dir=workspace, settings=resolved_settings)

    def live_result() -> Optional[WorkerStartResult]:
        health = store.worker_health()
        pid = int(health.get("pid") or 0)
        if not health.get("available") or pid <= 0:
            return None
        if not process_identity_matches(
            pid,
            expected_boot_id=str(health.get("boot_id") or ""),
            expected_start_identity=str(health.get("start_identity") or ""),
        ):
            return None
        return WorkerStartResult(True, str(health.get("state") or "running"), pid)

    current = live_result()
    if current is not None:
        return current

    spawn_lock = _acquire_lock(root / ".worker-spawn.lock", blocking=True)
    if spawn_lock is None:  # pragma: no cover - blocking acquisition returns a handle
        return WorkerStartResult(False, "unavailable", warning="could not lock worker startup")
    try:
        current = live_result()
        if current is not None:
            return current
        token = uuid.uuid4().hex
        command = [
            sys.executable,
            "-m",
            "xagent.interfaces.cli",
            "_run-jobs-worker",
            "--jobs-dir",
            str(root),
            "--workspace-dir",
            str(workspace),
            "--worker-token",
            token,
            "--settings-json",
            json.dumps(asdict(resolved_settings), separators=(",", ":")),
        ]
        log_path = root / "worker.log"
        try:
            log_handle = log_path.open("ab")
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=True,
                close_fds=True,
            )
            log_handle.close()
            identity = wait_for_process_start_identity(process.pid)
            if not identity:
                raise RuntimeError("could not establish worker process identity")
            store.set_worker_health(
                pid=process.pid,
                token=token,
                boot_id=boot_session_id(),
                start_identity=identity,
                state="starting",
            )
            return WorkerStartResult(True, "starting", process.pid)
        except Exception as exc:
            store.set_worker_health(
                pid=0,
                token=token,
                boot_id="",
                start_identity=None,
                state="unavailable",
                last_error=str(exc),
            )
            return WorkerStartResult(
                False,
                "unavailable",
                warning=f"job was queued but the worker could not start: {exc}",
            )
    finally:
        fcntl.flock(spawn_lock.fileno(), fcntl.LOCK_UN)
        spawn_lock.close()


class JobWorker:
    def __init__(
        self,
        *,
        jobs_dir: Path,
        workspace_dir: Path,
        worker_token: str,
        settings: JobSettings,
    ) -> None:
        self.jobs_dir = jobs_dir
        self.workspace_dir = workspace_dir
        self.worker_token = worker_token
        self.settings = settings
        self.store = JobStore(
            jobs_dir,
            workspace_dir=workspace_dir,
            settings=settings,
        )
        self.logger = logging.getLogger("xagent.jobs.worker")
        self.stop_requested = False
        self.lock_handle: Optional[IO[str]] = None

    def run(self) -> int:
        self.lock_handle = _acquire_lock(self.jobs_dir / ".worker.lock", blocking=False)
        if self.lock_handle is None:
            return 0
        self._install_signal_handlers()
        started = now_ms()
        identity = process_start_identity(os.getpid())
        self.store.set_worker_health(
            pid=os.getpid(),
            token=self.worker_token,
            boot_id=boot_session_id(),
            start_identity=identity,
            state="running",
            started_at_ms=started,
        )
        last_activity = time.monotonic()
        last_cleanup = 0.0
        try:
            while not self.stop_requested:
                recovered = self.store.reconcile_receipts()
                self._reconcile_stale_attempts()
                launched = 0
                while not self.stop_requested:
                    claim = self.store.claim_next(worker_token=self.worker_token)
                    if claim is None:
                        break
                    self._spawn_runner(claim.job_id, claim.attempt_id, claim.runner_token)
                    launched += 1
                counts = self.store.counts()
                if recovered or launched or counts["active"]:
                    last_activity = time.monotonic()
                state = "running" if counts["active"] else "idle"
                self.store.set_worker_health(
                    pid=os.getpid(),
                    token=self.worker_token,
                    boot_id=boot_session_id(),
                    start_identity=identity,
                    state=state,
                    started_at_ms=started,
                )
                if (
                    not counts["active"]
                    and time.monotonic() - last_activity >= self.settings.worker_idle_seconds
                ):
                    return 0
                if time.monotonic() - last_cleanup >= 60:
                    self.store.cleanup_retention()
                    last_cleanup = time.monotonic()
                time.sleep(0.25)
            return 0
        except Exception as exc:
            self.logger.exception("Job worker failed")
            self.store.set_worker_health(
                pid=os.getpid(),
                token=self.worker_token,
                boot_id=boot_session_id(),
                start_identity=identity,
                state="error",
                last_error=str(exc),
                started_at_ms=started,
            )
            return 1
        finally:
            if self.lock_handle is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(self.lock_handle.fileno(), fcntl.LOCK_UN)
                self.lock_handle.close()

    def _spawn_runner(self, job_id: str, attempt_id: str, runner_token: str) -> None:
        command = [
            sys.executable,
            "-m",
            "xagent.core.runtime.job_runner",
            "--jobs-dir",
            str(self.jobs_dir),
            "--workspace-dir",
            str(self.workspace_dir),
            "--job-id",
            job_id,
            "--attempt-id",
            attempt_id,
            "--runner-token",
            runner_token,
            "--settings-json",
            json.dumps(asdict(self.settings), separators=(",", ":")),
        ]
        log_handle = (self.jobs_dir / "worker.log").open("ab")
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            log_handle.close()
        self.store.set_runner_identity(
            attempt_id,
            runner_token=runner_token,
            pid=process.pid,
            boot_id=boot_session_id(),
            start_identity=wait_for_process_start_identity(process.pid),
        )

    def _reconcile_stale_attempts(self) -> None:
        stale_before = now_ms() - int(self.settings.runner_stale_seconds * 1000)
        for attempt in self.store.stale_attempts(stale_before_ms=stale_before):
            runner_pid = int(attempt.get("runner_pid") or 0)
            runner_boot = str(attempt.get("runner_boot_id") or "")
            runner_start = str(attempt.get("runner_start_identity") or "")
            if runner_pid and process_identity_matches(
                runner_pid,
                expected_boot_id=runner_boot,
                expected_start_identity=runner_start,
            ):
                best_effort_kill_verified_group(
                    runner_pid,
                    expected_boot_id=runner_boot,
                    expected_start_identity=runner_start,
                )
            child_pid = int(attempt.get("child_pid") or 0)
            child_boot = str(attempt.get("child_boot_id") or "")
            child_start = str(attempt.get("child_start_identity") or "")
            if child_pid:
                best_effort_kill_verified_group(
                    child_pid,
                    expected_boot_id=child_boot,
                    expected_start_identity=child_start,
                )
            self.store.mark_stale_attempt_terminal(
                str(attempt["id"]),
                runner_token=str(attempt["runner_token"]),
                cancelled=str(attempt.get("job_desired_state")) == "cancelled",
            )

    def _install_signal_handlers(self) -> None:
        def stop(_signum: int, _frame) -> None:
            self.stop_requested = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Internal xAgent background job worker")
    parser.add_argument("--jobs-dir", required=True)
    parser.add_argument("--workspace-dir", required=True)
    parser.add_argument("--worker-token", required=True)
    parser.add_argument("--settings-json", required=True)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    settings = JobSettings.from_mapping(json.loads(args.settings_json))
    worker = JobWorker(
        jobs_dir=Path(args.jobs_dir).expanduser().resolve(),
        workspace_dir=Path(args.workspace_dir).expanduser().resolve(),
        worker_token=args.worker_token,
        settings=settings,
    )
    return worker.run()


if __name__ == "__main__":
    sys.exit(main())
