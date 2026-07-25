"""Public background-job runtime built on the transactional local store."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

from .job_store import (
    ACTIVE_JOB_STATUSES,
    ATTENTION_JOB_STATUSES,
    DATABASE_FILENAME,
    JOB_KIND_PROCESS,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_CANCELLING,
    JOB_STATUS_FAILED,
    JOB_STATUS_INTERRUPTED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_STARTING,
    JOB_STATUS_SUCCEEDED,
    TERMINAL_JOB_STATUSES,
    IdempotencyConflict,
    JobRecord,
    JobSettings,
    JobStore,
    QueueCapacityError,
)
from .job_worker import WorkerStartResult, ensure_worker_running
from .tasks import ScheduledDeliveryContext, current_delivery_context


DEFAULT_JOB_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_MAX_CONCURRENT_JOBS = 2
DEFAULT_CANCEL_GRACE_SECONDS = 5.0


def settings_from_config(config: Optional[Mapping[str, Any]]) -> JobSettings:
    raw = config.get("jobs") if isinstance(config, Mapping) else None
    if raw is not None and not isinstance(raw, Mapping):
        raise ValueError("jobs must be a dictionary")
    return JobSettings.from_mapping(raw)


def ensure_jobs_dirs(jobs_dir: Path | str) -> tuple[Path, Path]:
    root = Path(jobs_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    # Compatibility return shape. New failures are represented transactionally.
    legacy_failed = root / "failed"
    return root, legacy_failed


def enqueue_job(
    *,
    kind: str = JOB_KIND_PROCESS,
    jobs_dir: Path | str,
    channel: str,
    target: dict[str, Any],
    command: Optional[str] = None,
    argv: Optional[Sequence[str]] = None,
    shell: Optional[bool] = None,
    user_id: str = "",
    title: str = "",
    cwd: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    timeout_seconds: Optional[int] = None,
    resources: Optional[Sequence[str]] = None,
    source: Optional[dict[str, Any]] = None,
    workspace_dir: Path | str | None = None,
    settings: Optional[JobSettings] = None,
    idempotency_scope: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    retry_of: Optional[str] = None,
) -> JobRecord:
    if str(kind or "").strip().lower() != JOB_KIND_PROCESS:
        raise ValueError("kind must be process")
    if env:
        raise ValueError("custom environment variables are not supported; job secrets must not be inherited")
    # The compatibility facade treats the legacy command-only call as an
    # explicit shell request. New public APIs always send shell themselves.
    resolved_shell = bool(command) if shell is None else bool(shell)
    resolved_settings = settings or JobSettings()
    store = JobStore(
        jobs_dir,
        workspace_dir=workspace_dir,
        settings=resolved_settings,
    )
    record = store.create_job(
        title=title,
        argv=argv,
        command=command,
        shell=resolved_shell,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        resources=resources,
        channel=channel,
        target=target,
        user_id=user_id,
        source=source,
        idempotency_scope=idempotency_scope,
        idempotency_key=idempotency_key,
        retry_of=retry_of,
    )
    ensure_worker_running(
        store.root,
        workspace_dir=store.workspace_dir,
        settings=resolved_settings,
    )
    return record


def list_job_records(
    jobs_dir: Path | str,
    *,
    include_failed: bool = True,
    include_archived: bool = False,
    include_claimed: bool = False,
) -> list[JobRecord]:
    del include_claimed
    scope = "all" if include_archived else ("current" if include_failed else "active")
    records, _total = JobStore(jobs_dir).list_jobs(scope=scope, limit=200, offset=0)
    if not include_failed:
        records = [record for record in records if record.status in ACTIVE_JOB_STATUSES]
    return records


def list_archived_job_records(jobs_dir: Path | str) -> list[JobRecord]:
    records, _total = JobStore(jobs_dir).list_jobs(scope="history", limit=200, offset=0)
    return records


def count_archived_job_records(jobs_dir: Path | str) -> int:
    return JobStore(jobs_dir).counts()["history"]


def get_job(jobs_dir: Path | str, job_id: str) -> JobRecord:
    return JobStore(jobs_dir).get_job(job_id)


def request_job_cancel(jobs_dir: Path | str, job_id: str) -> JobRecord:
    store = JobStore(jobs_dir)
    record = store.request_cancel(job_id)
    ensure_worker_running(
        store.root,
        workspace_dir=store.workspace_dir,
        settings=store.settings,
    )
    return record


def retry_job(
    jobs_dir: Path | str,
    job_id: str,
    *,
    idempotency_scope: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> JobRecord:
    store = JobStore(jobs_dir)
    record = store.retry_job(
        job_id,
        idempotency_scope=idempotency_scope,
        idempotency_key=idempotency_key,
    )
    ensure_worker_running(
        store.root,
        workspace_dir=store.workspace_dir,
        settings=store.settings,
    )
    return record


def delete_job(jobs_dir: Path | str, job_id: str) -> JobRecord:
    return JobStore(jobs_dir).delete_job(job_id)


def has_live_job_supervisor(jobs_dir: Path | str, *, channel: str = "") -> bool:
    del channel
    return bool(JobStore(jobs_dir).worker_health().get("available"))


class AsyncJobDeliveryDispatcher:
    """Durably deliver terminal job notifications for selected channels."""

    def __init__(
        self,
        jobs_dir: Path | str,
        *,
        channels: Sequence[str],
        can_notify: Callable[[JobRecord], bool],
        notify: Callable[[JobRecord], Awaitable[None]],
        poll_interval_seconds: float = DEFAULT_JOB_POLL_INTERVAL_SECONDS,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self.store = JobStore(jobs_dir)
        self.channels = tuple(str(item or "").strip().lower() for item in channels)
        self.can_notify = can_notify
        self.notify = notify
        self.poll_interval_seconds = max(0.05, float(poll_interval_seconds))
        self.logger = logger_ or logging.getLogger(__name__)
        self._task: Optional[asyncio.Task[None]] = None
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()

    def wake(self) -> None:
        self._wake_event.set()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def tick(self) -> int:
        delivered = 0
        for delivery in self.store.claim_deliveries(self.channels):
            try:
                job = self.store.get_job(delivery.job_id)
                if not self.can_notify(job):
                    raise RuntimeError(
                        f"channel {delivery.channel!r} cannot deliver this job"
                    )
                await self.notify(job)
            except Exception as exc:
                self.store.delivery_failed(
                    delivery.delivery_id,
                    str(exc),
                    attempt_count=delivery.attempt_count,
                )
                self.logger.warning(
                    "Job notification delivery failed for %s: %s",
                    delivery.job_id,
                    exc,
                )
                continue
            self.store.delivery_succeeded(delivery.delivery_id)
            delivered += 1
        return delivered

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(),
                    timeout=self.poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass
            self._wake_event.clear()


class AsyncJobSupervisor:
    """Compatibility adapter: starts the independent worker plus delivery.

    Execution no longer belongs to the channel lifecycle. Stopping this object
    only stops notification delivery and never cancels running jobs.
    """

    def __init__(
        self,
        jobs_dir: Path | str,
        *,
        can_notify: Callable[[JobRecord], bool],
        notify: Callable[[JobRecord], Awaitable[None]],
        can_handle: Callable[[JobRecord], bool] | None = None,
        owner_channels: Optional[Sequence[str]] = None,
        workspace_dir: Path | str | None = None,
        poll_interval_seconds: float = DEFAULT_JOB_POLL_INTERVAL_SECONDS,
        max_concurrent_jobs: int = DEFAULT_MAX_CONCURRENT_JOBS,
        cancel_grace_seconds: float = DEFAULT_CANCEL_GRACE_SECONDS,
        logger_: Optional[logging.Logger] = None,
        on_complete: Callable[[JobRecord], Awaitable[None]] | None = None,
    ) -> None:
        del can_handle, on_complete
        self.jobs_dir = Path(jobs_dir).expanduser().resolve()
        self.workspace_dir = workspace_dir
        self.settings = JobSettings(
            max_concurrent=max_concurrent_jobs,
            cancel_grace_seconds=cancel_grace_seconds,
        )
        self.delivery = AsyncJobDeliveryDispatcher(
            self.jobs_dir,
            channels=owner_channels or ("", "api", "local"),
            can_notify=can_notify,
            notify=notify,
            poll_interval_seconds=poll_interval_seconds,
            logger_=logger_,
        )

    def wake(self) -> None:
        ensure_worker_running(
            self.jobs_dir,
            workspace_dir=self.workspace_dir,
            settings=self.settings,
        )
        self.delivery.wake()

    async def start(self) -> None:
        ensure_worker_running(
            self.jobs_dir,
            workspace_dir=self.workspace_dir,
            settings=self.settings,
        )
        await self.delivery.start()

    async def stop(self) -> None:
        await self.delivery.stop()

    async def tick(self) -> int:
        self.wake()
        return await self.delivery.tick()

    def recover_orphaned_jobs(self) -> int:
        return JobStore(
            self.jobs_dir,
            workspace_dir=self.workspace_dir,
            settings=self.settings,
        ).reconcile_receipts()


def delivery_context_for_job_tool() -> ScheduledDeliveryContext:
    context = current_delivery_context()
    if context is not None:
        return context
    return ScheduledDeliveryContext(channel="local", user_id="", target={}, metadata={})


__all__ = [
    "ACTIVE_JOB_STATUSES",
    "ATTENTION_JOB_STATUSES",
    "DATABASE_FILENAME",
    "JOB_KIND_PROCESS",
    "JOB_STATUS_CANCELLED",
    "JOB_STATUS_CANCELLING",
    "JOB_STATUS_FAILED",
    "JOB_STATUS_INTERRUPTED",
    "JOB_STATUS_QUEUED",
    "JOB_STATUS_RUNNING",
    "JOB_STATUS_STARTING",
    "JOB_STATUS_SUCCEEDED",
    "TERMINAL_JOB_STATUSES",
    "AsyncJobDeliveryDispatcher",
    "AsyncJobSupervisor",
    "IdempotencyConflict",
    "JobRecord",
    "JobSettings",
    "JobStore",
    "QueueCapacityError",
    "WorkerStartResult",
    "count_archived_job_records",
    "delete_job",
    "enqueue_job",
    "ensure_jobs_dirs",
    "ensure_worker_running",
    "get_job",
    "has_live_job_supervisor",
    "list_archived_job_records",
    "list_job_records",
    "request_job_cancel",
    "retry_job",
    "settings_from_config",
]
