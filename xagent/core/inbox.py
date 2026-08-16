"""In-process inbox for classifying and serializing agent input.

This is a delivery/wakeup seam, not a memory store. Diary remains the only
long-term memory carrier. Observations persist without waking a turn.

Foreground work (human turns, due agent tasks, steer) shares a high-priority
FIFO. Initiative is low-priority: it can be overtaken while queued, and is
never preempted once the turn lease is granted. At most one initiative is
pending or running; it is not persisted across process restart.
"""

from __future__ import annotations

import asyncio
import heapq
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from .delivery import DeliveryContext

INBOX_KIND_METADATA_KEY = "inbox_kind"
TASK_CONTENT_METADATA_KEY = "task_content"
SCHEDULED_AGENT_PROMPT_PREFIX = (
    "This scheduled task is now due. Execute it and return the message to deliver.\n\n"
    "Task: "
)

_HIGH_PRIORITY = 0
_LOW_PRIORITY = 1


class InboxKind(str, Enum):
    """How an inbound item should be treated by the agent loop."""

    USER_TURN = "user_turn"
    SCHEDULED_TURN = "scheduled_turn"
    OBSERVATION = "observation"
    STEER = "steer"
    INITIATIVE_TURN = "initiative_turn"

    @property
    def wakes(self) -> bool:
        return self in {
            InboxKind.USER_TURN,
            InboxKind.SCHEDULED_TURN,
            InboxKind.STEER,
            InboxKind.INITIATIVE_TURN,
        }

    @property
    def stores_user_message(self) -> bool:
        return self in {
            InboxKind.USER_TURN,
            InboxKind.SCHEDULED_TURN,
            InboxKind.STEER,
        }

    @property
    def queue_priority(self) -> int:
        if self is InboxKind.INITIATIVE_TURN:
            return _LOW_PRIORITY
        return _HIGH_PRIORITY


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


def _new_item_id() -> str:
    return uuid.uuid4().hex


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
    item_id: str = field(default_factory=_new_item_id)
    created_at: datetime = field(default_factory=datetime.now)
    delivery: Optional[DeliveryContext] = None

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
        if self.kind is InboxKind.INITIATIVE_TURN:
            payload.setdefault("source", "initiative")
            payload.setdefault("intent", str(self.content or "").strip())
            if self.delivery is not None and self.delivery.recipient_key:
                payload.setdefault("recipient_key", self.delivery.recipient_key)
        return payload


@dataclass
class TurnLease:
    """Exclusive right to run one waking turn. Not preemptable once granted."""

    item: InboxItem


class AgentInbox:
    """Per-agent priority queue and turn lease.

    Human turns, due scheduled tasks, and steer share high-priority FIFO.
    Initiative is low-priority. A queued initiative can be overtaken; a
    started turn cannot. At most one initiative may be pending or running.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._cv = asyncio.Condition(self._lock)
        self._seq = 0
        self._heap: list[tuple[int, int, str]] = []
        self._pending: dict[str, InboxItem] = {}
        self._lease: Optional[TurnLease] = None
        self._initiative_id: Optional[str] = None

    @property
    def busy(self) -> bool:
        return self._lease is not None

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def has_initiative(self) -> bool:
        if self._initiative_id is None:
            return False
        if self._initiative_id in self._pending:
            return True
        if self._lease is not None and self._lease.item.item_id == self._initiative_id:
            return True
        return False

    async def acquire_turn(self, item: Optional[InboxItem] = None) -> Optional[TurnLease]:
        """Enqueue *item* and wait until it holds the exclusive turn lease.

        Returns ``None`` when a second initiative is dropped. ``item`` may be
        omitted only for the legacy lock-style call used by older tests; a
        synthetic high-priority user turn is then assumed.
        """
        queued = item if item is not None else InboxItem(
            kind=InboxKind.USER_TURN,
            content="",
            user_id="",
        )
        async with self._cv:
            if queued.kind is InboxKind.INITIATIVE_TURN and self.has_initiative():
                return None
            if queued.kind is InboxKind.INITIATIVE_TURN:
                self._initiative_id = queued.item_id
            seq = self._seq
            self._seq += 1
            heapq.heappush(
                self._heap,
                (queued.kind.queue_priority, seq, queued.item_id),
            )
            self._pending[queued.item_id] = queued
            try:
                while True:
                    if (
                        self._lease is None
                        and self._heap
                        and self._heap[0][2] == queued.item_id
                    ):
                        heapq.heappop(self._heap)
                        self._pending.pop(queued.item_id, None)
                        lease = TurnLease(item=queued)
                        self._lease = lease
                        return lease
                    await self._cv.wait()
            except asyncio.CancelledError:
                self._drop_pending(queued.item_id)
                self._cv.notify_all()
                raise

    async def release_turn(self) -> None:
        async with self._cv:
            lease = self._lease
            self._lease = None
            if lease is not None and lease.item.kind is InboxKind.INITIATIVE_TURN:
                if self._initiative_id == lease.item.item_id:
                    self._initiative_id = None
            self._cv.notify_all()

    def _drop_pending(self, item_id: str) -> None:
        self._pending.pop(item_id, None)
        self._heap = [entry for entry in self._heap if entry[2] != item_id]
        heapq.heapify(self._heap)
        if self._initiative_id == item_id and (
            self._lease is None or self._lease.item.item_id != item_id
        ):
            self._initiative_id = None
