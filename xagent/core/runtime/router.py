"""Lightweight main-loop Router: accept Triggers, dispatch async Workers.

The Router is the only path that should address the user. Workers report
through the TriggerBus as ``JOB_RESULT`` events; this module drains them
back into the main loop without speaking on the Worker's behalf.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .async_jobs import AsyncJobRunner, JobHandle, JobCallable
from .triggers import (
    FastPathDecision,
    FastPathFilter,
    FastPathResult,
    Trigger,
    TriggerBus,
    TriggerKind,
    make_trigger,
)

logger = logging.getLogger(__name__)

OutboundSink = Callable[[Trigger], Awaitable[None] | None]
JobResultHandler = Callable[[Trigger], Awaitable[None] | None]


@dataclass
class RouterAdmission:
    """Result of admitting an inbound Trigger through Fast Path."""

    trigger: Trigger
    fast_path: FastPathResult
    accepted: bool


@dataclass
class RouterState:
    """Snapshot of Router queues for debugging."""

    pending_triggers: int = 0
    active_jobs: int = 0
    recent_drops: List[Dict[str, Any]] = field(default_factory=list)


class AgentRouter:
    """Main-session Router: Fast Path admission + async Worker dispatch.

    Design notes (GOAL-aligned):
    - One subject identity; Workers do not become separate user-facing agents.
    - Observations vs requests stay distinguished via ``Trigger.kind``.
    - Actor / channel / thread attribution is preserved on every Trigger.
    - Diary remains authoritative; this Router does not write memory itself.
    """

    def __init__(
        self,
        *,
        trigger_bus: Optional[TriggerBus] = None,
        fast_path: Optional[FastPathFilter] = None,
        job_runner: Optional[AsyncJobRunner] = None,
        outbound_sink: Optional[OutboundSink] = None,
        job_result_handler: Optional[JobResultHandler] = None,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self.bus = trigger_bus or TriggerBus()
        self.fast_path = fast_path or FastPathFilter(decision_log=[])
        self.jobs = job_runner or AsyncJobRunner(self.bus)
        self._outbound_sink = outbound_sink
        self._job_result_handler = job_result_handler
        self._logger = logger_ or logger
        self._drop_log: List[Dict[str, Any]] = []

    def admit(self, trigger: Trigger) -> RouterAdmission:
        """Enqueue after Fast Path. DROPs are logged and not queued."""
        decision = self.fast_path.evaluate(trigger)
        if decision.decision is FastPathDecision.DROP:
            record = {
                "trigger_id": trigger.id,
                "kind": trigger.kind.value,
                "reason": decision.reason,
                "rule": decision.rule,
            }
            self._drop_log.append(record)
            self._logger.info(
                "Router dropped trigger id=%s kind=%s rule=%s reason=%s",
                trigger.id,
                trigger.kind.value,
                decision.rule,
                decision.reason,
            )
            return RouterAdmission(trigger=trigger, fast_path=decision, accepted=False)
        self.bus.enqueue(trigger)
        return RouterAdmission(trigger=trigger, fast_path=decision, accepted=True)

    def admit_direct_request(
        self,
        *,
        content: str,
        actor_id: str,
        channel: str,
        thread_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        source: Optional[str] = None,
    ) -> RouterAdmission:
        trigger = make_trigger(
            source=source or channel or "router",
            kind=TriggerKind.DIRECT_REQUEST,
            actor_id=actor_id,
            channel=channel,
            thread_id=thread_id,
            payload_summary=(content or "")[:240],
            payload={"content": content, **dict(payload or {})},
        )
        return self.admit(trigger)

    def start_job(
        self,
        name: str,
        work: JobCallable,
        *,
        channel: Optional[str] = None,
        actor_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> JobHandle:
        """Dispatch a Worker. Returns Started immediately; main loop stays free."""
        return self.jobs.start(
            name,
            work,
            channel=channel,
            actor_id=actor_id,
            thread_id=thread_id,
            metadata=metadata,
        )

    def stop_job(self, job_id: str) -> bool:
        return self.jobs.stop(job_id)

    async def drain_and_route(self, *, max_items: Optional[int] = None) -> List[Trigger]:
        """Drain pending Triggers and route JOB_RESULT events to the main loop.

        Non-result Triggers are returned to the caller (main session) for
        handling. Workers never call ``outbound_sink`` themselves.
        """
        batch = self.bus.drain(max_items)
        leftovers: List[Trigger] = []
        for trigger in batch:
            if trigger.kind is TriggerKind.JOB_RESULT:
                await self._handle_job_result(trigger)
            else:
                leftovers.append(trigger)
        return leftovers

    async def _handle_job_result(self, trigger: Trigger) -> None:
        if self._job_result_handler is not None:
            result = self._job_result_handler(trigger)
            if hasattr(result, "__await__"):
                await result  # type: ignore[misc]
            return
        # Default: hand a short completion note to the outbound sink if any.
        # Still Router-owned — never a Worker speaking directly.
        if self._outbound_sink is None:
            self._logger.info(
                "Job result ready id=%s summary=%.120s (no outbound sink)",
                (trigger.payload or {}).get("job_id"),
                trigger.payload_summary,
            )
            return
        result = self._outbound_sink(trigger)
        if hasattr(result, "__await__"):
            await result  # type: ignore[misc]

    def state(self) -> RouterState:
        decision_log = getattr(self.fast_path, "_decision_log", None) or []
        recent_drops = [
            row for row in list(decision_log)[-20:]
            if row.get("decision") == FastPathDecision.DROP.value
        ]
        recent_drops.extend(self._drop_log[-20:])
        return RouterState(
            pending_triggers=self.bus.pending_count,
            active_jobs=self.jobs.active_count,
            recent_drops=recent_drops[-20:],
        )
