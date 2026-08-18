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
TASK_CONTENT_METADATA_KEY = "task_content"
SCHEDULED_AGENT_PROMPT_PREFIX = (
    "This scheduled task is now due. Execute it and return the message to deliver.\n\n"
    "Task: "
)


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


def scheduled_task_display_content(
    content: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Return the task body, without the model-instruction wrapper."""
    payload = metadata or {}
    stored = str(payload.get(TASK_CONTENT_METADATA_KEY) or "").strip()
    if stored:
        return stored
    text = str(content or "")
    if text.startswith(SCHEDULED_AGENT_PROMPT_PREFIX):
        return text[len(SCHEDULED_AGENT_PROMPT_PREFIX) :].strip()
    return text.strip()


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
            payload.setdefault(
                TASK_CONTENT_METADATA_KEY,
                scheduled_task_display_content(self.content, payload),
            )
        return payload


class AgentInbox:
    """Per-agent turn lock so one identity never interleaves two live turns."""

    def __init__(self) -> None:
        self._turn_lock = asyncio.Lock()
        self._abort = asyncio.Event()

    @property
    def busy(self) -> bool:
        return self._turn_lock.locked()

    def abort_requested(self) -> bool:
        return self._abort.is_set()

    def request_abort(self) -> bool:
        """Ask the in-flight turn to stop at the next iteration boundary.

        Returns True when a turn is busy and the request was recorded.
        Idle calls are a no-op.
        """
        if not self._turn_lock.locked():
            return False
        self._abort.set()
        return True

    async def acquire_turn(self) -> None:
        await self._turn_lock.acquire()
        self._abort.clear()

    def release_turn(self) -> None:
        self._abort.clear()
        if self._turn_lock.locked():
            self._turn_lock.release()
