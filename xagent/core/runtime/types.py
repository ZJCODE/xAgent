"""Provider-neutral events and deliveries owned by one agent runtime."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


MAX_EVENT_CONTENT_BYTES = 65_536
MAX_PENDING_EVENTS_PER_SOURCE = 100
DELIVERY_CHANNELS = frozenset({"api", "feishu", "weixin", "voice"})
LOCAL_OWNER_PERSON_ID = "owner"

EVENT_STATUS_PENDING = "pending"
EVENT_STATUS_PROCESSING = "processing"
EVENT_STATUS_COMPLETED = "completed"
EVENT_STATUS_FAILED = "failed"
EVENT_STATUS_NEEDS_REVIEW = "needs_review"

DELIVERY_STATUS_PENDING = "pending"
DELIVERY_STATUS_SENDING = "sending"
DELIVERY_STATUS_DELIVERED = "delivered"
DELIVERY_STATUS_BLOCKED = "blocked"
DELIVERY_STATUS_FAILED = "failed"
DELIVERY_STATUS_UNKNOWN = "unknown"


class RuntimeBacklogFull(RuntimeError):
    """Raised when one event source exhausts its bounded pending-event budget."""


@dataclass(frozen=True)
class AgentEvent:
    """One attributable experience submitted to the single agent timeline."""

    event_id: str
    kind: str
    source: str
    conversation_id: str
    speaker_id: str
    audience_ids: tuple[str, ...]
    content: str
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.event_id or "").strip():
            raise ValueError("event_id is required")
        if not str(self.kind or "").strip():
            raise ValueError("kind is required")
        if not str(self.source or "").strip():
            raise ValueError("source is required")
        if not str(self.speaker_id or "").strip():
            raise ValueError("speaker_id is required")
        if len(self.content.encode("utf-8", errors="replace")) > MAX_EVENT_CONTENT_BYTES:
            raise ValueError(f"event content exceeds {MAX_EVENT_CONTENT_BYTES} bytes")

    @classmethod
    def create(
        cls,
        *,
        kind: str,
        source: str,
        speaker_id: str,
        content: str,
        conversation_id: str = "",
        audience_ids: tuple[str, ...] = (),
        metadata: dict[str, Any] | None = None,
        event_id: str | None = None,
        timestamp: float | None = None,
    ) -> "AgentEvent":
        return cls(
            event_id=event_id or uuid.uuid4().hex,
            kind=kind,
            source=source,
            conversation_id=conversation_id,
            speaker_id=speaker_id,
            audience_ids=tuple(audience_ids),
            content=content,
            timestamp=time.time() if timestamp is None else float(timestamp),
            metadata=dict(metadata or {}),
        )


@dataclass(frozen=True)
class StoredEvent:
    sequence: int
    event: AgentEvent
    status: str
    side_effect_started: bool = False
    result: dict[str, Any] | None = None
    error: str = ""


@dataclass(frozen=True)
class Delivery:
    """One durable outward action produced by the runtime."""

    delivery_id: str
    event_id: str
    channel: str
    target: dict[str, Any]
    payload: dict[str, Any]
    status: str = DELIVERY_STATUS_PENDING
    attempts: int = 0
    channel_message_id: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        channel = str(self.channel or "").strip().lower()
        if channel not in DELIVERY_CHANNELS:
            allowed = ", ".join(sorted(DELIVERY_CHANNELS))
            raise ValueError(
                f"delivery channel must be one of {allowed}; got {self.channel!r}"
            )
        if not str(self.delivery_id or "").strip():
            raise ValueError("delivery_id is required")
        if not str(self.event_id or "").strip():
            raise ValueError("event_id is required")

    @classmethod
    def create(
        cls,
        *,
        event_id: str,
        channel: str,
        target: dict[str, Any],
        payload: dict[str, Any],
        delivery_id: str | None = None,
        status: str = DELIVERY_STATUS_PENDING,
    ) -> "Delivery":
        return cls(
            delivery_id=delivery_id or uuid.uuid4().hex,
            event_id=event_id,
            channel=channel,
            target=dict(target),
            payload=dict(payload),
            status=status,
        )
