"""In-process inbox for classifying and serializing agent input.

This is a delivery/wakeup seam, not a memory store. Diary remains the only
long-term memory carrier. Observations persist without waking a turn.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Union

INBOX_KIND_METADATA_KEY = "inbox_kind"


class InboxKind(str, Enum):
    """How an inbound item should be treated by the agent loop."""

    USER_TURN = "user_turn"
    SCHEDULED_TURN = "scheduled_turn"
    OBSERVATION = "observation"
    STEER = "steer"

    @property
    def wakes(self) -> bool:
        return self in {InboxKind.USER_TURN, InboxKind.SCHEDULED_TURN, InboxKind.STEER}


def is_scheduled_work(metadata: Optional[Dict[str, Any]] = None) -> bool:
    """True when a stored message is a due task, not a human utterance."""
    payload = metadata or {}
    kind = str(payload.get(INBOX_KIND_METADATA_KEY) or "").strip()
    if kind == InboxKind.SCHEDULED_TURN.value:
        return True
    return str(payload.get("source") or "").strip() == "scheduled_task"


def normalize_inbox_kind(
    value: Optional[Union[InboxKind, str]],
    *,
    default: InboxKind = InboxKind.USER_TURN,
) -> InboxKind:
    """Coerce a caller-supplied kind to ``InboxKind``."""
    if value is None:
        return default
    if isinstance(value, InboxKind):
        return value
    raw = str(value).strip()
    if not raw:
        return default
    try:
        return InboxKind(raw)
    except ValueError:
        return default


@dataclass
class InboxItem:
    """One classified inbound item awaiting persistence and/or a turn."""

    kind: InboxKind
    content: str
    user_id: str
    channel: Optional[str] = None
    room_name: Optional[str] = None
    attachments: Optional[List[Dict[str, Any]]] = None
    image_source: Optional[Union[str, List[str]]] = None
    channel_instructions: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    stream: bool = False

    @property
    def wakeup(self) -> bool:
        return self.kind.wakes

    def message_metadata(self) -> Dict[str, Any]:
        """Metadata persisted on the stored user message or context event."""
        payload = dict(self.metadata or {})
        payload[INBOX_KIND_METADATA_KEY] = self.kind.value
        if self.kind is InboxKind.SCHEDULED_TURN:
            payload.setdefault("source", "scheduled_task")
        return payload


class AgentInbox:
    """Per-agent turn lock so one identity never interleaves two live turns."""

    def __init__(self) -> None:
        self._turn_lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        return self._turn_lock.locked()

    async def acquire_turn(self) -> None:
        await self._turn_lock.acquire()

    def release_turn(self) -> None:
        if self._turn_lock.locked():
            self._turn_lock.release()
