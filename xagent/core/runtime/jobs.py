"""File-backed background jobs that run outside chat turns."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .scheduler import ARCHIVE_DIRNAME, FAILED_DIRNAME, _fsync_directory, _unique_failed_path
from .tasks import ScheduledDeliveryContext, current_delivery_context


JOB_KIND_PROCESS = "process"
JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"
JOB_PAYLOAD_VERSION = 1
JOB_JSON_SUFFIX = ".json"
CLAIM_MARKER = ".claimed-"
DEFAULT_MAX_CONCURRENT_JOBS = 2
DEFAULT_JOB_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_CANCEL_GRACE_SECONDS = 5.0
DEFAULT_LOG_TAIL_BYTES = 4096
MAX_JOB_LOG_BYTES = 2 * 1024 * 1024
SUPPORTED_JOB_KINDS = {JOB_KIND_PROCESS}
ACTIVE_JOB_STATUSES = {JOB_STATUS_QUEUED, JOB_STATUS_RUNNING}
TERMINAL_JOB_STATUSES = {JOB_STATUS_COMPLETED, JOB_STATUS_FAILED, JOB_STATUS_CANCELLED}


@dataclass(frozen=True)
class JobRecord:
    """Structured view of one background job file."""

    path: Path
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def job_id(self) -> str:
        raw = self.payload.get("id") if isinstance(self.payload, dict) else None
        return str(raw or self.path.stem.split(CLAIM_MARKER, 1)[0])

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def title(self) -> str:
        return str(self.payload.get("title") or "").strip()

    @property
    def kind(self) -> str:
        return str(self.payload.get("kind") or "").strip().lower()

    @property
    def status(self) -> str:
        return str(self.payload.get("status") or JOB_STATUS_QUEUED).strip().lower()

    @property
    def spec(self) -> dict[str, Any]:
        raw = self.payload.get("spec")
        return dict(raw) if isinstance(raw, dict) else {}

    @property
    def delivery(self) -> dict[str, Any]:
        raw = self.payload.get("delivery")
        return dict(raw) if isinstance(raw, dict) else {}

    @property
    def delivery_channel(self) -> str:
        return str(self.delivery.get("channel") or "").strip()

    @property
    def delivery_user_id(self) -> str:
        return str(self.delivery.get("user_id") or "").strip()

    @property
    def target(self) -> dict[str, Any]:
        delivery = self.delivery
        target = delivery.get("target")
        result = dict(target) if isinstance(target, dict) else {}
        channel = self.delivery_channel
        user_id = self.delivery_user_id
        if channel:
            result.setdefault("channel", channel)
        if user_id:
            result.setdefault("user_id", user_id)
        return result

    @property
    def command(self) -> str:
        return str(self.spec.get("command") or "").strip()

    @property
    def resources(self) -> list[str]:
        raw = self.spec.get("resources")
        if not isinstance(raw, list):
            return []
        return [str(item).strip() for item in raw if str(item).strip()]

    @property
    def work_dir(self) -> Path:
        jobs_root = self.path.parent
        if CLAIM_MARKER in self.path.name or jobs_root.name == FAILED_DIRNAME or ARCHIVE_DIRNAME in self.path.parts:
            jobs_root = _jobs_root_from_path(self.path)
        return jobs_root / self.job_id / "work"

    @property
    def log_dir(self) -> Path:
        jobs_root = _jobs_root_from_path(self.path)
        return jobs_root / self.job_id

    def to_job_view(self, *, log_tail: bool = False) -> dict[str, Any]:
        view = {
            "job_id": self.job_id,
            "title": self.title or "Background job",
            "kind": self.kind or JOB_KIND_PROCESS,
            "status": self.status,
            "command": self.command,
            "cwd": self.spec.get("cwd"),
            "timeout_seconds": self.spec.get("timeout_seconds"),
            "resources": self.resources,
            "channel": self.delivery_channel or "local",
            "user_id": self.delivery_user_id,
            "target": self.target,
            "progress": dict(self.payload.get("progress") or {}),
            "execution": dict(self.payload.get("execution") or {}),
            "result": dict(self.payload.get("result") or {}),
            "last_error": self.payload.get("last_error"),
            "created_at": self.payload.get("created_at"),
            "updated_at": self.payload.get("updated_at"),
            "started_at": self.payload.get("started_at"),
            "completed_at": self.payload.get("completed_at"),
            "failed_at": self.payload.get("failed_at"),
            "cancelled_at": self.payload.get("cancelled_at"),
        }
        if log_tail:
            view["stdout_tail"] = _read_log_tail(self.log_dir / "stdout.log")
            view["stderr_tail"] = _read_log_tail(self.log_dir / "stderr.log")
        return view


def ensure_jobs_dirs(jobs_dir: Path | str) -> tuple[Path, Path]:
    """Ensure the jobs root and failed directory exist."""
    root = Path(jobs_dir).expanduser().resolve()
    failed = root / FAILED_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    failed.mkdir(parents=True, exist_ok=True)
    return root, failed


def enqueue_job(
    *,
    kind: str,
    command: str,
    jobs_dir: Path | str,
    channel: str,
    target: dict[str, Any],
    user_id: str = "",
    title: str = "",
    cwd: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    timeout_seconds: Optional[int] = None,
    resources: Optional[list[str]] = None,
    source: Optional[dict[str, Any]] = None,
) -> JobRecord:
    """Atomically enqueue a background process job."""
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind not in SUPPORTED_JOB_KINDS:
        raise ValueError(f"kind must be one of: {', '.join(sorted(SUPPORTED_JOB_KINDS))}")
    normalized_command = str(command or "").strip()
    if not normalized_command:
        raise ValueError("job command must not be empty")
    if timeout_seconds is not None:
        timeout_seconds = int(timeout_seconds)
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive when provided")

    root, _failed = ensure_jobs_dirs(jobs_dir)
    job_id = uuid.uuid4().hex
    work_dir = root / job_id / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    now = _now_text()
    payload = {
        "version": JOB_PAYLOAD_VERSION,
        "id": job_id,
        "title": title.strip() if title else "",
        "kind": normalized_kind,
        "status": JOB_STATUS_QUEUED,
        "spec": {
            "command": normalized_command,
            "cwd": str(cwd).strip() if cwd else str(work_dir),
            "env": dict(env or {}),
            "timeout_seconds": timeout_seconds,
            "resources": [str(item).strip() for item in (resources or []) if str(item).strip()],
        },
        "delivery": {
            "channel": str(channel or "").strip(),
            "target": dict(target or {}),
            "user_id": user_id,
        },
        "progress": {},
        "execution": {},
        "result": {},
        "source": dict(source or {}),
        "created_at": now,
        "updated_at": now,
    }
    path = root / f"{job_id}{JOB_JSON_SUFFIX}"
    _write_json_atomic(path, payload)
    return JobRecord(path=path, payload=payload)


def list_job_records(
    jobs_dir: Path | str,
    *,
    include_failed: bool = True,
    include_archived: bool = False,
    include_claimed: bool = False,
) -> list[JobRecord]:
    """Return job records from the jobs control plane."""
    root, failed = ensure_jobs_dirs(jobs_dir)
    records: list[JobRecord] = []
    for path in sorted(root.glob(f"*{JOB_JSON_SUFFIX}"), key=lambda item: item.name):
        record = _record_from_json_file(path)
        if record is not None:
            records.append(record)
    if include_claimed:
        for path in sorted(root.glob(f"*{JOB_JSON_SUFFIX}{CLAIM_MARKER}*"), key=lambda item: item.name):
            record = _record_from_json_file(path)
            if record is not None:
                records.append(record)
    if include_failed and failed.is_dir():
        for path in sorted(failed.glob(f"*{JOB_JSON_SUFFIX}*"), key=lambda item: item.name):
            if JOB_JSON_SUFFIX not in path.name:
                continue
            record = _record_from_json_file(path)
            if record is not None:
                records.append(record)
    if include_archived:
        records.extend(list_archived_job_records(jobs_dir))
    return sorted(records, key=lambda item: str(item.payload.get("created_at") or ""), reverse=True)


def list_archived_job_records(jobs_dir: Path | str) -> list[JobRecord]:
    root, _failed = ensure_jobs_dirs(jobs_dir)
    archive = root / ARCHIVE_DIRNAME
    records: list[JobRecord] = []
    if archive.is_dir():
        for path in archive.rglob(f"*{JOB_JSON_SUFFIX}"):
            record = _record_from_json_file(path)
            if record is not None:
                records.append(record)
    return sorted(records, key=lambda item: str(item.payload.get("completed_at") or item.payload.get("updated_at") or ""), reverse=True)


def count_archived_job_records(jobs_dir: Path | str) -> int:
    root, _failed = ensure_jobs_dirs(jobs_dir)
    archive = root / ARCHIVE_DIRNAME
    if not archive.is_dir():
        return 0
    return sum(1 for path in archive.rglob(f"*{JOB_JSON_SUFFIX}") if path.is_file())


def get_job(jobs_dir: Path | str, job_id: str) -> JobRecord:
    normalized = str(job_id or "").strip()
    if not normalized:
        raise ValueError("job_id is required")
    for record in list_job_records(jobs_dir, include_archived=True, include_claimed=True):
        if record.job_id == normalized:
            return record
    raise FileNotFoundError(f"job not found: {normalized}")


def delete_job(jobs_dir: Path | str, job_id: str) -> JobRecord:
    record = get_job(jobs_dir, job_id)
    if record.status == JOB_STATUS_RUNNING:
        raise ValueError("cannot delete a running job; cancel it first")
    record.path.unlink(missing_ok=True)
    return record


def request_job_cancel(jobs_dir: Path | str, job_id: str) -> JobRecord:
    """Mark a job for cancellation; supervisor performs the actual kill."""
    record = get_job(jobs_dir, job_id)
    if record.status in TERMINAL_JOB_STATUSES:
        return record
    now = _now_text()
    payload = dict(record.payload)
    if record.status == JOB_STATUS_QUEUED:
        payload["status"] = JOB_STATUS_CANCELLED
        payload["cancelled_at"] = now
        payload["updated_at"] = now
        payload["result"] = {"summary": "Cancelled before start"}
        _write_json_atomic(record.path, payload)
        archived = _archive_job_path(record.path, _jobs_root_from_path(record.path), payload)
        return JobRecord(path=archived, payload=payload)

    payload["cancel_requested"] = True
    payload["updated_at"] = now
    _write_json_atomic(record.path, payload)
    return JobRecord(path=record.path, payload=payload)


class AsyncJobSupervisor:
    """Claim and execute queued process jobs without holding chat capacity."""

    def __init__(
        self,
        jobs_dir: Path | str,
        *,
        can_notify: Callable[[JobRecord], bool],
        notify: Callable[[JobRecord], Awaitable[None]],
        workspace_dir: Path | str | None = None,
        poll_interval_seconds: float = DEFAULT_JOB_POLL_INTERVAL_SECONDS,
        max_concurrent_jobs: int = DEFAULT_MAX_CONCURRENT_JOBS,
        cancel_grace_seconds: float = DEFAULT_CANCEL_GRACE_SECONDS,
        logger_: Optional[logging.Logger] = None,
        on_complete: Callable[[JobRecord], Awaitable[None]] | None = None,
    ):
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if max_concurrent_jobs <= 0:
            raise ValueError("max_concurrent_jobs must be positive")
        self.jobs_dir, self.failed_dir = ensure_jobs_dirs(jobs_dir)
        self.workspace_dir = Path(workspace_dir).expanduser().resolve() if workspace_dir else None
        self.can_notify = can_notify
        self.notify = notify
        self.on_complete = on_complete
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.max_concurrent_jobs = int(max_concurrent_jobs)
        self.cancel_grace_seconds = float(cancel_grace_seconds)
        self.logger = logger_ or logging.getLogger(__name__)
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        self._inflight: dict[str, asyncio.Task[None]] = {}
        self._resource_locks: dict[str, asyncio.Lock] = {}
        self._resource_owners: dict[str, str] = {}

    def wake(self) -> None:
        self._wake_event.set()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self.recover_orphaned_jobs()
        self._task = asyncio.create_task(self.run_forever())

    async def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        task = self._task
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._task = None
        if self._inflight:
            await asyncio.gather(*list(self._inflight.values()), return_exceptions=True)
        self._inflight.clear()

    def recover_orphaned_jobs(self) -> int:
        recovered = 0
        for path in sorted(self.jobs_dir.glob(f"*{JOB_JSON_SUFFIX}{CLAIM_MARKER}*"), key=lambda item: item.name):
            original_name = path.name.split(CLAIM_MARKER, 1)[0]
            destination = self.jobs_dir / original_name
            record = _record_from_json_file(path)
            if record is None:
                self._quarantine(path, original_name, "invalid")
                continue
            pid = int((record.payload.get("execution") or {}).get("pid") or 0)
            if pid > 0 and _pid_is_running(pid):
                # Another supervisor still owns this process; leave claimed.
                continue
            payload = dict(record.payload)
            payload["status"] = JOB_STATUS_FAILED
            payload["failed_at"] = _now_text()
            payload["updated_at"] = _now_text()
            payload["last_error"] = "job process lost after runtime restart"
            payload["result"] = {"summary": "Recovered orphaned running job after restart"}
            payload.pop("cancel_requested", None)
            try:
                _write_json_atomic(path, payload)
                if destination.exists():
                    self._quarantine(path, original_name, "orphaned")
                else:
                    path.rename(destination)
                    failed_path = self._move_to_failed(destination, "orphaned")
                    _ = failed_path
                recovered += 1
            except OSError as exc:
                self.logger.error("failed to recover orphaned job %s: %s", path.name, exc)
        for record in list_job_records(self.jobs_dir, include_failed=False, include_claimed=False):
            if record.status != JOB_STATUS_RUNNING:
                continue
            pid = int((record.payload.get("execution") or {}).get("pid") or 0)
            if pid > 0 and _pid_is_running(pid):
                continue
            payload = dict(record.payload)
            payload["status"] = JOB_STATUS_FAILED
            payload["failed_at"] = _now_text()
            payload["updated_at"] = _now_text()
            payload["last_error"] = "job marked running but process is not alive"
            _write_json_atomic(record.path, payload)
            self._move_to_failed(record.path, "orphaned")
            recovered += 1
        return recovered

    async def run_forever(self) -> None:
        self.logger.info("background job supervisor started: jobs=%s", self.jobs_dir)
        while not self._stop_event.is_set():
            try:
                await self.tick()
                await self._wait_for_next_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.exception("job supervisor loop error: %s", exc)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_interval_seconds)
                except asyncio.TimeoutError:
                    pass
        self.logger.info("background job supervisor stopped")

    async def _wait_for_next_cycle(self) -> None:
        self._wake_event.clear()
        stop_wait = asyncio.create_task(self._stop_event.wait())
        wake_wait = asyncio.create_task(self._wake_event.wait())
        try:
            done, pending = await asyncio.wait(
                {stop_wait, wake_wait},
                timeout=self.poll_interval_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            _ = done
        except asyncio.CancelledError:
            stop_wait.cancel()
            wake_wait.cancel()
            await asyncio.gather(stop_wait, wake_wait, return_exceptions=True)
            raise

    async def tick(self) -> None:
        self._reap_finished_inflight()
        for record in list_job_records(self.jobs_dir, include_failed=False, include_claimed=False):
            if record.status != JOB_STATUS_QUEUED:
                continue
            if bool(record.payload.get("cancel_requested")):
                request_job_cancel(self.jobs_dir, record.job_id)
                continue
            if len(self._inflight) >= self.max_concurrent_jobs:
                break
            if not self._resources_available(record):
                continue
            claimed_path = self._claim(record.path)
            if claimed_path is None:
                continue
            claimed = _record_from_json_file(claimed_path) or record
            task = asyncio.create_task(self._run_claimed(claimed_path, claimed))
            self._inflight[claimed.job_id] = task
            task.add_done_callback(lambda done, job_id=claimed.job_id: self._on_job_done(job_id, done))

    def _reap_finished_inflight(self) -> None:
        finished = [job_id for job_id, task in self._inflight.items() if task.done()]
        for job_id in finished:
            self._inflight.pop(job_id, None)

    def _on_job_done(self, job_id: str, task: asyncio.Task[None]) -> None:
        self._inflight.pop(job_id, None)
        self._wake_event.set()
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.logger.exception("background job task crashed (%s): %s", job_id, exc)

    def _resources_available(self, record: JobRecord) -> bool:
        for resource in record.resources:
            owner = self._resource_owners.get(resource)
            if owner and owner != record.job_id:
                return False
        return True

    async def _acquire_resources(self, record: JobRecord) -> list[str]:
        acquired: list[str] = []
        for resource in record.resources:
            lock = self._resource_locks.setdefault(resource, asyncio.Lock())
            await lock.acquire()
            self._resource_owners[resource] = record.job_id
            acquired.append(resource)
        return acquired

    def _release_resources(self, resources: list[str], job_id: str) -> None:
        for resource in resources:
            if self._resource_owners.get(resource) == job_id:
                self._resource_owners.pop(resource, None)
            lock = self._resource_locks.get(resource)
            if lock is not None and lock.locked():
                lock.release()

    async def _run_claimed(self, claimed_path: Path, claimed: JobRecord) -> None:
        acquired = await self._acquire_resources(claimed)
        final_record: Optional[JobRecord] = None
        try:
            if claimed.kind != JOB_KIND_PROCESS:
                raise ValueError(f"unsupported job kind: {claimed.kind}")
            final_record = await self._run_process_job(claimed_path, claimed)
        except Exception as exc:
            self.logger.exception("background job failed -> %s: %s", claimed.job_id, exc)
            final_record = self._fail_claimed(claimed_path, claimed, exc)
        finally:
            self._release_resources(acquired, claimed.job_id)
        if final_record is None:
            return
        try:
            if self.on_complete is not None:
                await self.on_complete(final_record)
        except Exception as exc:
            self.logger.exception("job on_complete hook failed for %s: %s", final_record.job_id, exc)
        if self.can_notify(final_record):
            try:
                await self.notify(final_record)
            except Exception as exc:
                self.logger.exception("job notify failed for %s: %s", final_record.job_id, exc)

    async def _run_process_job(self, claimed_path: Path, claimed: JobRecord) -> JobRecord:
        cwd = self._resolve_cwd(claimed)
        env = os.environ.copy()
        extra_env = claimed.spec.get("env")
        if isinstance(extra_env, dict):
            for key, value in extra_env.items():
                env[str(key)] = str(value)
        log_dir = self.jobs_dir / claimed.job_id
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = log_dir / "stdout.log"
        stderr_path = log_dir / "stderr.log"
        timeout_seconds = claimed.spec.get("timeout_seconds")
        timeout = float(timeout_seconds) if timeout_seconds is not None else None

        now = _now_text()
        payload = dict(claimed.payload)
        payload["status"] = JOB_STATUS_RUNNING
        payload["started_at"] = now
        payload["updated_at"] = now
        payload["progress"] = {"message": "running", "updated_at": now}
        _write_json_atomic(claimed_path, payload)

        stdout_handle = stdout_path.open("ab")
        stderr_handle = stderr_path.open("ab")
        process: Optional[asyncio.subprocess.Process] = None
        try:
            process = await asyncio.create_subprocess_shell(
                claimed.command,
                cwd=str(cwd),
                env=env,
                stdout=stdout_handle,
                stderr=stderr_handle,
            )
            payload["execution"] = {
                "pid": process.pid,
                "started_at": now,
            }
            payload["updated_at"] = _now_text()
            _write_json_atomic(claimed_path, payload)
            (log_dir / "pid").write_text(f"{process.pid}\n", encoding="utf-8")

            returncode = await self._wait_process_with_cancel(claimed_path, claimed.job_id, process, timeout)
            ended = _now_text()
            payload = _read_payload(claimed_path) or payload
            cancel_requested = bool(payload.get("cancel_requested"))
            payload["execution"] = {
                **dict(payload.get("execution") or {}),
                "pid": process.pid,
                "ended_at": ended,
                "return_code": returncode,
            }
            payload["updated_at"] = ended
            payload.pop("cancel_requested", None)
            if cancel_requested:
                payload["status"] = JOB_STATUS_CANCELLED
                payload["cancelled_at"] = ended
                payload["result"] = {
                    "summary": "Cancelled",
                    "exit_code": returncode,
                    "stdout_log": str(stdout_path),
                    "stderr_log": str(stderr_path),
                }
            elif returncode == 0:
                payload["status"] = JOB_STATUS_COMPLETED
                payload["completed_at"] = ended
                payload["result"] = {
                    "summary": "Completed successfully",
                    "exit_code": returncode,
                    "stdout_log": str(stdout_path),
                    "stderr_log": str(stderr_path),
                }
                payload.pop("last_error", None)
            else:
                payload["status"] = JOB_STATUS_FAILED
                payload["failed_at"] = ended
                payload["last_error"] = f"command exited with code {returncode}"
                payload["result"] = {
                    "summary": f"Failed with exit code {returncode}",
                    "exit_code": returncode,
                    "stdout_log": str(stdout_path),
                    "stderr_log": str(stderr_path),
                }
            _write_json_atomic(claimed_path, payload)
            if payload["status"] == JOB_STATUS_FAILED:
                final_path = self._move_to_failed(claimed_path, "failed")
            else:
                final_path = _archive_job_path(claimed_path, self.jobs_dir, payload)
            return JobRecord(path=final_path, payload=payload)
        finally:
            stdout_handle.close()
            stderr_handle.close()
            _truncate_log_file(stdout_path)
            _truncate_log_file(stderr_path)

    async def _wait_process_with_cancel(
        self,
        claimed_path: Path,
        job_id: str,
        process: asyncio.subprocess.Process,
        timeout: Optional[float],
    ) -> int:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout if timeout is not None else None
        while True:
            if self._stop_event.is_set():
                await _terminate_process(process, self.cancel_grace_seconds)
                return process.returncode if process.returncode is not None else -1
            payload = _read_payload(claimed_path) or {}
            if bool(payload.get("cancel_requested")):
                await _terminate_process(process, self.cancel_grace_seconds)
                return process.returncode if process.returncode is not None else -15
            if deadline is not None and loop.time() >= deadline:
                await _terminate_process(process, self.cancel_grace_seconds)
                return process.returncode if process.returncode is not None else -9
            try:
                return await asyncio.wait_for(process.wait(), timeout=self.poll_interval_seconds)
            except asyncio.TimeoutError:
                continue

    def _resolve_cwd(self, record: JobRecord) -> Path:
        raw = str(record.spec.get("cwd") or "").strip()
        candidate = Path(raw).expanduser() if raw else record.work_dir
        if not candidate.is_absolute():
            base = self.workspace_dir or self.jobs_dir
            candidate = (base / candidate).resolve()
        else:
            candidate = candidate.resolve()
        allowed_roots = [self.jobs_dir]
        if self.workspace_dir is not None:
            allowed_roots.append(self.workspace_dir)
        if not any(_is_relative_to(candidate, root) for root in allowed_roots):
            raise ValueError("job cwd must stay inside jobs/ or workspace/")
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate

    def _fail_claimed(self, claimed_path: Path, claimed: JobRecord, error: Exception) -> JobRecord:
        now = _now_text()
        payload = dict(claimed.payload)
        payload.update(
            {
                "status": JOB_STATUS_FAILED,
                "failed_at": now,
                "updated_at": now,
                "last_error": _safe_error_summary(error),
                "result": {"summary": _safe_error_summary(error)},
            }
        )
        payload.pop("cancel_requested", None)
        try:
            _write_json_atomic(claimed_path, payload)
        except OSError as exc:
            self.logger.error("failed to persist job failure for %s: %s", claimed.job_id, exc)
        final_path = self._move_to_failed(claimed_path, "failed")
        return JobRecord(path=final_path, payload=payload)

    def _claim(self, path: Path) -> Optional[Path]:
        for _attempt in range(8):
            claimed_path = path.with_name(f"{path.name}{CLAIM_MARKER}{uuid.uuid4().hex[:8]}")
            if claimed_path.exists():
                continue
            try:
                path.rename(claimed_path)
                return claimed_path
            except FileNotFoundError:
                return None
            except OSError as exc:
                self.logger.error("failed to claim job %s: %s", path.name, exc)
                return None
        self.logger.error("failed to claim job %s: could not reserve claim name", path.name)
        return None

    def _quarantine(self, path: Path, original_name: str, reason: str) -> None:
        self.failed_dir.mkdir(parents=True, exist_ok=True)
        destination = _unique_failed_path(self.failed_dir, original_name, reason)
        try:
            path.rename(destination)
        except FileNotFoundError:
            return
        except OSError as exc:
            self.logger.error("failed to quarantine job %s: %s", original_name, exc)

    def _move_to_failed(self, path: Path, reason: str) -> Path:
        original_name = path.name.split(CLAIM_MARKER, 1)[0]
        self.failed_dir.mkdir(parents=True, exist_ok=True)
        destination = _unique_failed_path(self.failed_dir, original_name, reason)
        try:
            path.rename(destination)
            return destination
        except FileNotFoundError:
            return path
        except OSError as exc:
            self.logger.error("failed to move job to failed/: %s", exc)
            return path


def _jobs_root_from_path(path: Path) -> Path:
    resolved = path.resolve()
    parts = list(resolved.parts)
    if FAILED_DIRNAME in parts:
        idx = parts.index(FAILED_DIRNAME)
        return Path(*parts[:idx])
    if ARCHIVE_DIRNAME in parts:
        idx = parts.index(ARCHIVE_DIRNAME)
        return Path(*parts[:idx])
    return resolved.parent


def _archive_job_path(path: Path, root: Path, payload: dict[str, Any]) -> Path:
    stamp = str(payload.get("completed_at") or payload.get("cancelled_at") or payload.get("updated_at") or _now_text())
    try:
        completed_at = datetime.strptime(stamp[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        completed_at = datetime.now().replace(microsecond=0)
    archive_dir = root / ARCHIVE_DIRNAME / completed_at.strftime("%Y-%m")
    archive_dir.mkdir(parents=True, exist_ok=True)
    job_id = str(payload.get("id") or path.stem)[:8]
    candidate = archive_dir / f"{completed_at.strftime('%Y%m%d-%H%M%S')}-{job_id}{JOB_JSON_SUFFIX}"
    if candidate.exists():
        candidate = archive_dir / f"{completed_at.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}{JOB_JSON_SUFFIX}"
    try:
        os.link(path, candidate)
        path.unlink(missing_ok=True)
        _fsync_directory(archive_dir)
        return candidate
    except OSError:
        path.rename(candidate)
        return candidate


def _record_from_json_file(path: Path) -> Optional[JobRecord]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return JobRecord(path=path, payload=raw)


def _read_payload(path: Path) -> Optional[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)
    _fsync_directory(path.parent)


def _now_text() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _safe_error_summary(error: Exception) -> str:
    text = " ".join(str(error).split()).strip() or error.__class__.__name__
    text = re.sub(
        r"(?i)\b(api[_-]?key|access[_-]?token|token|secret|password)\b\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]",
        text,
    )
    return text[:500]


def _read_log_tail(path: Path, max_bytes: int = DEFAULT_LOG_TAIL_BYTES) -> str:
    if not path.is_file():
        return ""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            data = handle.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


def _truncate_log_file(path: Path) -> None:
    if not path.is_file():
        return
    try:
        size = path.stat().st_size
        if size <= MAX_JOB_LOG_BYTES:
            return
        data = path.read_bytes()[-MAX_JOB_LOG_BYTES:]
        path.write_bytes(data)
    except OSError:
        return


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _terminate_process(process: asyncio.subprocess.Process, grace_seconds: float) -> None:
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.send_signal(signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=max(0.1, grace_seconds))
        return
    except asyncio.TimeoutError:
        pass
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=2.0)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def delivery_context_for_job_tool() -> ScheduledDeliveryContext:
    """Helper used by tools; exposed for tests."""
    context = current_delivery_context()
    if context is not None:
        return context
    return ScheduledDeliveryContext(channel="local", user_id="", target={}, metadata={})
