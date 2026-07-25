"""Background job notification helpers for the api channel."""

from __future__ import annotations

import logging
from typing import Optional

from ...core.agent import Agent
from ...core.runtime import JobRecord
from .constants import CHANNEL_API
from .delivery import DeliveryBus


class JobNotifyService:
    """Notify api subscribers when a background job reaches a terminal state."""

    def __init__(
        self,
        agent: Agent,
        *,
        delivery: DeliveryBus,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.agent = agent
        self.delivery = delivery
        self.logger = logger or logging.getLogger(self.__class__.__name__)

    def can_notify(self, job: JobRecord) -> bool:
        return job.delivery_channel in {"", CHANNEL_API, "local"}

    async def notify(self, job: JobRecord) -> None:
        summary = _job_summary(job)
        await self._observe_completion(job, summary)
        stored_message = await self._store_completion_once(job, summary)
        await self.delivery.broadcast_job_message(
            job,
            summary,
            stored_message=stored_message,
        )

    async def _store_completion_once(self, job: JobRecord, summary: str):
        message_handler = getattr(self.agent, "message_handler", None)
        store_model_reply = getattr(message_handler, "store_model_reply", None)
        if not callable(store_model_reply):
            return None
        return await store_model_reply(
            summary,
            getattr(self.agent, "_assistant_sender_id", "agent"),
            metadata={
                "background_job": {
                    "job_id": job.job_id,
                    "status": job.status,
                    "delivery_key": f"job:{job.job_id}:api",
                }
            },
            channel=CHANNEL_API,
            recipient_id=job.delivery_user_id or str(job.target.get("user_id") or ""),
            dedupe_key=f"job:{job.job_id}:api",
        )

    async def _observe_completion(self, job: JobRecord, summary: str) -> None:
        observe = getattr(self.agent, "observe", None)
        if not callable(observe):
            return
        try:
            await observe(
                context=summary,
                source="background_job",
                event_type="job_succeeded" if job.status == "succeeded" else f"job_{job.status}",
                metadata={
                    "job_id": job.job_id,
                    "status": job.status,
                    "title": job.title,
                },
                channel=CHANNEL_API,
            )
        except Exception:
            self.logger.debug("Failed to observe job completion for %s", job.job_id, exc_info=True)


def _job_summary(job: JobRecord) -> str:
    title = job.title or "Background job"
    result = job.payload.get("result") if isinstance(job.payload.get("result"), dict) else {}
    detail = str((result or {}).get("summary") or job.payload.get("last_error") or job.status).strip()
    return f"[job {job.status}] {title}: {detail} (id={job.job_id})"
