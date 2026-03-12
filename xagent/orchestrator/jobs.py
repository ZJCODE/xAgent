"""Background job execution and cancellation."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional

from ..state import ConversationStateStore, JobRecord


JobEventCallback = Optional[Callable[[str, Dict[str, Any]], Awaitable[None] | None]]


class JobManager:
    """Tracks background execution tasks for orchestrated work."""

    def __init__(self, state_store: ConversationStateStore):
        self.state_store = state_store
        self._tasks: Dict[str, asyncio.Task] = {}

    async def start_job(
        self,
        conversation_id: str,
        turn_id: str,
        runner: Callable[[], Awaitable[Any]],
        on_event: JobEventCallback = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> JobRecord:
        job = JobRecord(
            job_id=f"job_{uuid.uuid4().hex[:10]}",
            conversation_id=conversation_id,
            turn_id=turn_id,
            status="queued",
            metadata=metadata or {},
        )
        self.state_store.upsert_job(job)
        if on_event is not None:
            maybe_awaitable = on_event("job.started", {"job_id": job.job_id, "turn_id": turn_id})
            if maybe_awaitable is not None:
                await maybe_awaitable

        async def run_job() -> None:
            job.status = "running"
            self.state_store.upsert_job(job)
            if on_event is not None:
                maybe_awaitable = on_event(
                    "job.progress",
                    {
                        "job_id": job.job_id,
                        "turn_id": turn_id,
                        "status": "running",
                    },
                )
                if maybe_awaitable is not None:
                    await maybe_awaitable
            try:
                result = await runner()
                job.status = "completed"
                job.result_text = str(result)
                self.state_store.upsert_job(job)
                if on_event is not None:
                    maybe_awaitable = on_event(
                        "job.completed",
                        {
                            "job_id": job.job_id,
                            "turn_id": turn_id,
                            "result": str(result),
                        },
                    )
                    if maybe_awaitable is not None:
                        await maybe_awaitable
            except asyncio.CancelledError:
                job.status = "cancelled"
                self.state_store.upsert_job(job)
                if on_event is not None:
                    maybe_awaitable = on_event(
                        "job.failed",
                        {
                            "job_id": job.job_id,
                            "turn_id": turn_id,
                            "error": "cancelled",
                        },
                    )
                    if maybe_awaitable is not None:
                        await maybe_awaitable
                raise
            except Exception as exc:
                job.status = "failed"
                job.error = str(exc)
                self.state_store.upsert_job(job)
                if on_event is not None:
                    maybe_awaitable = on_event(
                        "job.failed",
                        {
                            "job_id": job.job_id,
                            "turn_id": turn_id,
                            "error": str(exc),
                        },
                    )
                    if maybe_awaitable is not None:
                        await maybe_awaitable

        self._tasks[job.job_id] = asyncio.create_task(run_job(), name=f"xagent:{job.job_id}")
        return job

    def get_job(self, job_id: str) -> Optional[JobRecord]:
        return self.state_store.get_job(job_id)

    async def cancel_job(self, job_id: str) -> Optional[JobRecord]:
        task = self._tasks.get(job_id)
        if task is None:
            return self.state_store.get_job(job_id)

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return self.state_store.get_job(job_id)
