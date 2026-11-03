"""Async Worker job runner: start/stop with results re-injected as Triggers.

Workers never speak to the user directly. Job completion enqueues a
``TriggerKind.JOB_RESULT`` onto the shared TriggerBus so the main Router
loop can unify outbound replies.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Optional

from .triggers import TriggerBus, TriggerKind, make_trigger

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    STARTED = "started"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass
class JobHandle:
    """Handle returned immediately from ``AsyncJobRunner.start``."""

    job_id: str
    name: str
    status: JobStatus
    created_at: datetime = field(default_factory=lambda: datetime.now().astimezone())
    finished_at: Optional[datetime] = None
    result_summary: str = ""
    error: str = ""
    channel: Optional[str] = None
    actor_id: Optional[str] = None
    thread_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_started_payload(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "name": self.name,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(sep=" "),
            "channel": self.channel,
            "actor_id": self.actor_id,
            "thread_id": self.thread_id,
        }


JobCallable = Callable[[], Awaitable[Any]]


class AsyncJobRunner:
    """Run long work off the main turn; complete as Trigger bus events."""

    def __init__(
        self,
        trigger_bus: TriggerBus,
        *,
        source: str = "async_worker",
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self._bus = trigger_bus
        self._source = source
        self._logger = logger_ or logger
        self._jobs: Dict[str, JobHandle] = {}
        self._tasks: Dict[str, asyncio.Task[Any]] = {}
        self._results: Dict[str, Any] = {}

    @property
    def active_count(self) -> int:
        return sum(
            1
            for handle in self._jobs.values()
            if handle.status in {JobStatus.STARTED, JobStatus.RUNNING}
        )

    def get(self, job_id: str) -> Optional[JobHandle]:
        return self._jobs.get(job_id)

    def start(
        self,
        name: str,
        work: JobCallable,
        *,
        channel: Optional[str] = None,
        actor_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        job_id: Optional[str] = None,
    ) -> JobHandle:
        """Schedule ``work`` and return a Started handle immediately.

        ``work`` must be an async zero-arg callable (or partial). Results are
        enqueued as ``JOB_RESULT`` Triggers; Workers must not address the user.
        """
        if not callable(work):
            raise TypeError("work must be an async callable")
        resolved_id = str(job_id or uuid.uuid4().hex)
        if resolved_id in self._jobs:
            raise ValueError(f"job_id already exists: {resolved_id}")
        handle = JobHandle(
            job_id=resolved_id,
            name=str(name or "job"),
            status=JobStatus.STARTED,
            channel=channel,
            actor_id=actor_id,
            thread_id=thread_id,
            metadata=dict(metadata or {}),
        )
        self._jobs[resolved_id] = handle
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            handle.status = JobStatus.FAILED
            handle.error = "no running event loop"
            handle.finished_at = datetime.now().astimezone()
            raise RuntimeError("AsyncJobRunner.start requires a running event loop") from exc

        task = loop.create_task(self._run_job(handle, work), name=f"xagent-job-{resolved_id}")
        self._tasks[resolved_id] = task
        self._logger.info(
            "Async job started id=%s name=%s channel=%s actor=%s",
            resolved_id,
            handle.name,
            channel,
            actor_id,
        )
        return handle

    def stop(self, job_id: str) -> bool:
        """Request cancellation of a running job. Returns True if a task was cancelled."""
        task = self._tasks.get(job_id)
        handle = self._jobs.get(job_id)
        if task is None or handle is None:
            return False
        if task.done():
            return False
        task.cancel()
        self._logger.info("Async job stop requested id=%s name=%s", job_id, handle.name)
        return True

    async def wait(self, job_id: str, timeout: Optional[float] = None) -> JobHandle:
        task = self._tasks.get(job_id)
        handle = self._jobs.get(job_id)
        if handle is None:
            raise KeyError(job_id)
        if task is None:
            return handle
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            pass
        return self._jobs[job_id]

    async def shutdown(self, *, timeout: float = 5.0) -> None:
        """Cancel remaining jobs and wait briefly for them to settle."""
        for job_id in list(self._tasks):
            self.stop(job_id)
        pending = [task for task in self._tasks.values() if not task.done()]
        if not pending:
            return
        done, still = await asyncio.wait(pending, timeout=max(0.0, timeout))
        for task in still:
            task.cancel()
        if still:
            await asyncio.gather(*still, return_exceptions=True)

    async def _run_job(self, handle: JobHandle, work: JobCallable) -> None:
        handle.status = JobStatus.RUNNING
        started = time.monotonic()
        try:
            result = await work()
        except asyncio.CancelledError:
            handle.status = JobStatus.STOPPED
            handle.error = "cancelled"
            handle.finished_at = datetime.now().astimezone()
            self._enqueue_result(handle, result=None)
            raise
        except Exception as exc:
            handle.status = JobStatus.FAILED
            handle.error = f"{type(exc).__name__}: {exc}"
            handle.finished_at = datetime.now().astimezone()
            handle.result_summary = handle.error[:240]
            self._logger.warning(
                "Async job failed id=%s name=%s error=%s",
                handle.job_id,
                handle.name,
                handle.error,
                exc_info=True,
            )
            self._enqueue_result(handle, result=None)
            return

        handle.status = JobStatus.COMPLETED
        handle.finished_at = datetime.now().astimezone()
        self._results[handle.job_id] = result
        handle.result_summary = _summarize_result(result)
        elapsed = time.monotonic() - started
        self._logger.info(
            "Async job completed id=%s name=%s elapsed=%.3fs summary=%.80s",
            handle.job_id,
            handle.name,
            elapsed,
            handle.result_summary,
        )
        self._enqueue_result(handle, result=result)

    def _enqueue_result(self, handle: JobHandle, *, result: Any) -> None:
        trigger = make_trigger(
            source=self._source,
            kind=TriggerKind.JOB_RESULT,
            actor_id=handle.actor_id,
            channel=handle.channel,
            thread_id=handle.thread_id,
            payload_summary=handle.result_summary or handle.error or handle.status.value,
            payload={
                "job_id": handle.job_id,
                "name": handle.name,
                "status": handle.status.value,
                "result": result if _is_jsonish(result) else handle.result_summary,
                "error": handle.error,
                "metadata": dict(handle.metadata),
            },
            metadata={
                "worker_speaks_to_user": False,
                "route_via_main_loop": True,
            },
        )
        self._bus.enqueue(trigger)


def _summarize_result(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result.strip()[:500]
    if isinstance(result, dict):
        for key in ("summary", "content", "message", "text"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:500]
        return str(result)[:500]
    return str(result)[:500]


def _is_jsonish(value: Any) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_jsonish(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return all(_is_jsonish(item) for item in value)
    return False
