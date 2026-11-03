"""In-process Trigger bus and deterministic Fast Path filter.

Triggers are attributed environment / channel events. Diary remains the
long-term memory carrier; this module is a delivery and wakeup seam only.

Fast Path runs before Slow Path (LLM worthy / recipient_hint) so drops are
explainable instead of silent gate failures.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Union

logger = logging.getLogger(__name__)


class TriggerKind(str, Enum):
    """What kind of inbound signal this Trigger carries."""

    DIRECT_REQUEST = "direct_request"
    OBSERVATION = "observation"
    DEVICE = "device"
    TIMER = "timer"
    JOB_RESULT = "job_result"
    SUBCONSCIOUS_CANDIDATE = "subconscious_candidate"


class FastPathDecision(str, Enum):
    """Deterministic Fast Path outcome before any Slow Path LLM call."""

    ALLOW = "allow"
    DROP = "drop"
    DEFER_TO_SLOW_PATH = "defer_to_slow_path"


@dataclass(frozen=True)
class Trigger:
    """One attributed event on the Trigger bus."""

    id: str
    source: str
    kind: TriggerKind
    actor_id: Optional[str] = None
    channel: Optional[str] = None
    thread_id: Optional[str] = None
    payload_summary: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now().astimezone())
    payload: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "kind": self.kind.value,
            "actor_id": self.actor_id,
            "channel": self.channel,
            "thread_id": self.thread_id,
            "payload_summary": self.payload_summary,
            "created_at": self.created_at.isoformat(sep=" "),
            "payload": dict(self.payload or {}),
            "metadata": dict(self.metadata or {}),
        }


@dataclass(frozen=True)
class FastPathResult:
    """Explainable Fast Path decision."""

    decision: FastPathDecision
    reason: str
    rule: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "rule": self.rule,
        }


def new_trigger_id() -> str:
    return uuid.uuid4().hex


def make_trigger(
    *,
    source: str,
    kind: Union[TriggerKind, str],
    actor_id: Optional[str] = None,
    channel: Optional[str] = None,
    thread_id: Optional[str] = None,
    payload_summary: str = "",
    payload: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    trigger_id: Optional[str] = None,
    created_at: Optional[datetime] = None,
) -> Trigger:
    """Build a Trigger with normalized kind and fresh id when omitted."""
    if isinstance(kind, TriggerKind):
        resolved_kind = kind
    else:
        resolved_kind = TriggerKind(str(kind).strip())
    return Trigger(
        id=str(trigger_id or new_trigger_id()),
        source=str(source or "").strip() or "unknown",
        kind=resolved_kind,
        actor_id=(str(actor_id).strip() if actor_id is not None and str(actor_id).strip() else None),
        channel=(str(channel).strip().lower() if channel is not None and str(channel).strip() else None),
        thread_id=(str(thread_id).strip() if thread_id is not None and str(thread_id).strip() else None),
        payload_summary=str(payload_summary or "").strip(),
        created_at=created_at or datetime.now().astimezone(),
        payload=dict(payload or {}),
        metadata=dict(metadata or {}),
    )


TriggerHandler = Callable[[Trigger], Any]


class TriggerBus:
    """Process-local FIFO Trigger queue with optional fan-out subscribers."""

    def __init__(self, *, max_pending: int = 1024) -> None:
        self._max_pending = max(1, int(max_pending))
        self._queue: List[Trigger] = []
        self._subscribers: List[TriggerHandler] = []
        self._lock = threading.RLock()
        self._dropped = 0

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._queue)

    @property
    def dropped_count(self) -> int:
        with self._lock:
            return self._dropped

    def enqueue(self, trigger: Trigger) -> str:
        """Append a Trigger. Returns its id. Drops oldest when over capacity."""
        with self._lock:
            if len(self._queue) >= self._max_pending:
                self._queue.pop(0)
                self._dropped += 1
                logger.warning(
                    "TriggerBus capacity reached (%s); dropped oldest trigger",
                    self._max_pending,
                )
            self._queue.append(trigger)
            subscribers = list(self._subscribers)
        for handler in subscribers:
            try:
                handler(trigger)
            except Exception:
                logger.warning("Trigger subscriber failed for %s", trigger.id, exc_info=True)
        return trigger.id

    def subscribe(self, handler: TriggerHandler) -> Callable[[], None]:
        """Register a fan-out listener. Returns an unsubscribe callback."""
        if not callable(handler):
            raise TypeError("handler must be callable")
        with self._lock:
            self._subscribers.append(handler)

        def _unsubscribe() -> None:
            with self._lock:
                try:
                    self._subscribers.remove(handler)
                except ValueError:
                    return

        return _unsubscribe

    def drain(self, max_items: Optional[int] = None) -> List[Trigger]:
        """Remove and return up to ``max_items`` queued Triggers (FIFO)."""
        with self._lock:
            if not self._queue:
                return []
            if max_items is None or max_items >= len(self._queue):
                batch, self._queue = self._queue, []
                return batch
            take = max(0, int(max_items))
            batch, self._queue = self._queue[:take], self._queue[take:]
            return batch

    def peek(self, max_items: Optional[int] = None) -> List[Trigger]:
        """Return queued Triggers without removing them."""
        with self._lock:
            if max_items is None:
                return list(self._queue)
            take = max(0, int(max_items))
            return list(self._queue[:take])

    def clear(self) -> int:
        with self._lock:
            count = len(self._queue)
            self._queue.clear()
            return count


class FastPathFilter:
    """Deterministic pre-LLM filter: allow / drop / defer_to_slow_path.

    Rules are ordered and first match wins for DROP. When nothing drops and
    the Trigger still needs model judgment, the result is DEFER_TO_SLOW_PATH.
    Clear direct requests with an actor may ALLOW immediately.
    """

    def __init__(
        self,
        *,
        allowed_channels: Optional[Iterable[str]] = None,
        quiet_hours: Optional[Sequence[int]] = None,
        paired_device_ids: Optional[Union[Set[str], Callable[[], Set[str]]]] = None,
        require_actor_for_direct: bool = True,
        clock: Optional[Callable[[], datetime]] = None,
        decision_log: Optional[List[Dict[str, Any]]] = None,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self._allowed_channels = {
            str(channel).strip().lower()
            for channel in (allowed_channels or [])
            if str(channel).strip()
        }
        self._quiet_hours = self._normalize_quiet_hours(quiet_hours)
        self._paired_device_ids = paired_device_ids
        self._require_actor_for_direct = bool(require_actor_for_direct)
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._decision_log = decision_log
        self._logger = logger_ or logger

    def set_allowed_channels(self, channels: Iterable[str]) -> None:
        self._allowed_channels = {
            str(channel).strip().lower() for channel in channels if str(channel).strip()
        }

    def set_quiet_hours(self, quiet_hours: Optional[Sequence[int]]) -> None:
        self._quiet_hours = self._normalize_quiet_hours(quiet_hours)

    def set_paired_device_ids(
        self,
        paired_device_ids: Optional[Union[Set[str], Callable[[], Set[str]]]],
    ) -> None:
        self._paired_device_ids = paired_device_ids

    def evaluate(self, trigger: Trigger) -> FastPathResult:
        result = self._evaluate(trigger)
        record = {
            "trigger_id": trigger.id,
            "kind": trigger.kind.value,
            "source": trigger.source,
            "channel": trigger.channel,
            "actor_id": trigger.actor_id,
            **result.to_dict(),
            "logged_at": time.time(),
        }
        if self._decision_log is not None:
            self._decision_log.append(record)
        if result.decision is FastPathDecision.DROP:
            self._logger.info(
                "FastPath DROP trigger_id=%s kind=%s rule=%s reason=%s channel=%s actor=%s",
                trigger.id,
                trigger.kind.value,
                result.rule,
                result.reason,
                trigger.channel,
                trigger.actor_id,
            )
        else:
            self._logger.debug(
                "FastPath %s trigger_id=%s kind=%s rule=%s reason=%s",
                result.decision.value,
                trigger.id,
                trigger.kind.value,
                result.rule,
                result.reason,
            )
        return result

    def _evaluate(self, trigger: Trigger) -> FastPathResult:
        kind = trigger.kind

        if kind is TriggerKind.DIRECT_REQUEST and self._require_actor_for_direct:
            if not (trigger.actor_id or "").strip():
                return FastPathResult(
                    FastPathDecision.DROP,
                    reason="direct_request requires actor attribution",
                    rule="require_actor_for_direct",
                )

        if kind is TriggerKind.DEVICE:
            device_id = str(
                (trigger.payload or {}).get("device_id")
                or trigger.actor_id
                or ""
            ).strip()
            paired = self._resolve_paired_devices()
            if paired is not None:
                if not device_id:
                    return FastPathResult(
                        FastPathDecision.DROP,
                        reason="device trigger missing device_id",
                        rule="device_requires_id",
                    )
                if device_id not in paired:
                    return FastPathResult(
                        FastPathDecision.DROP,
                        reason=f"device {device_id!r} is not paired",
                        rule="device_not_paired",
                    )

        if trigger.channel and self._allowed_channels:
            if trigger.channel not in self._allowed_channels:
                return FastPathResult(
                    FastPathDecision.DROP,
                    reason=f"channel {trigger.channel!r} not in allowlist",
                    rule="channel_allowlist",
                )

        if kind is TriggerKind.SUBCONSCIOUS_CANDIDATE:
            hint = str((trigger.payload or {}).get("recipient_hint") or "").strip()
            if not hint:
                return FastPathResult(
                    FastPathDecision.DROP,
                    reason="empty recipient_hint; never guess another person",
                    rule="empty_recipient",
                )
            if self._in_quiet_hours():
                return FastPathResult(
                    FastPathDecision.DROP,
                    reason="quiet hours block proactive outbound delivery",
                    rule="quiet_hours",
                )
            # Hint present and channels open: Slow Path already produced worthy;
            # ALLOW means deterministic gates passed for delivery attempt.
            return FastPathResult(
                FastPathDecision.ALLOW,
                reason="candidate passed deterministic delivery gates",
                rule="subconscious_candidate_ok",
            )

        if kind in {TriggerKind.TIMER, TriggerKind.OBSERVATION, TriggerKind.JOB_RESULT}:
            if kind is TriggerKind.TIMER and self._in_quiet_hours():
                # Still allow diary reflection Slow Path; only mark quiet for
                # callers that consult metadata. Defer rather than drop so
                # inner life continues during quiet hours.
                return FastPathResult(
                    FastPathDecision.DEFER_TO_SLOW_PATH,
                    reason="quiet hours; defer timer to Slow Path (diary ok, outbound gated later)",
                    rule="quiet_hours_defer",
                )
            return FastPathResult(
                FastPathDecision.DEFER_TO_SLOW_PATH,
                reason="event needs Slow Path judgment",
                rule="needs_slow_path",
            )

        if kind is TriggerKind.DIRECT_REQUEST:
            return FastPathResult(
                FastPathDecision.ALLOW,
                reason="attributed direct request",
                rule="direct_request_allow",
            )

        return FastPathResult(
            FastPathDecision.DEFER_TO_SLOW_PATH,
            reason="default defer",
            rule="default",
        )

    def _resolve_paired_devices(self) -> Optional[Set[str]]:
        paired = self._paired_device_ids
        if paired is None:
            return None
        if callable(paired):
            try:
                resolved = paired()
            except Exception:
                self._logger.warning("paired_device_ids callback failed", exc_info=True)
                return set()
            return {str(item).strip() for item in (resolved or set()) if str(item).strip()}
        return {str(item).strip() for item in paired if str(item).strip()}

    def _in_quiet_hours(self) -> bool:
        if self._quiet_hours is None:
            return False
        start, end = self._quiet_hours
        hour = int(self._clock().hour)
        if start == end:
            return True
        if start < end:
            return start <= hour < end
        # Wraps midnight, e.g. 23 -> 7
        return hour >= start or hour < end

    @staticmethod
    def _normalize_quiet_hours(
        quiet_hours: Optional[Sequence[int]],
    ) -> Optional[tuple[int, int]]:
        if quiet_hours is None:
            return None
        values = list(quiet_hours)
        if len(values) != 2:
            raise ValueError("quiet_hours must be a (start_hour, end_hour) pair")
        start, end = int(values[0]), int(values[1])
        if not (0 <= start <= 23 and 0 <= end <= 23):
            raise ValueError("quiet_hours hours must be in 0..23")
        return start, end
