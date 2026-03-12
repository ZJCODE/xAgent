"""Realtime interruption handling."""

from __future__ import annotations

from ..orchestrator import JobManager
from ..state import ConversationStateStore


class InterruptController:
    """Cancels active playback and background jobs for a live session."""

    def __init__(self, state_store: ConversationStateStore, job_manager: JobManager):
        self.state_store = state_store
        self.job_manager = job_manager

    async def interrupt(self, realtime_session_id: str) -> None:
        session = self.state_store.mark_interrupted(realtime_session_id, interrupted=True)
        if session.active_job_id:
            await self.job_manager.cancel_job(session.active_job_id)
            self.state_store.set_active_job(realtime_session_id, None)
