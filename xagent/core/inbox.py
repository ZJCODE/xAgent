"""In-process inbox for classifying and serializing agent input.

This is a delivery/wakeup seam, not a memory store. Diary remains the only
long-term memory carrier. Observations persist without waking a turn.
"""

from __future__ import annotations

import asyncio
import time
import uuid
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

    def __init__(self, *, max_pending: int = 32, queue_timeout: float = 30.0, run_timeout: float = 600.0) -> None:
        self._turn_lock = asyncio.Lock()
        self._abort = asyncio.Event()
        self.max_pending = max_pending
        self.queue_timeout = queue_timeout
        self.run_timeout = run_timeout
        self._pending: dict[str, TurnTicket] = {}
        self._current: TurnTicket | None = None
        self._steering: list[str] = []
        self.accepting = True

    @property
    def busy(self) -> bool:
        return self._turn_lock.locked()

    def abort_requested(self) -> bool:
        return self._abort.is_set()

    def request_abort(self, turn_id: str | None = None, *, channel: str | None = None) -> bool:
        """Ask the in-flight turn to stop at the next iteration boundary.

        Returns True when a turn is busy and the request was recorded.
        Idle calls are a no-op.
        """
        current = self._current
        if current is not None and (turn_id is None or current.turn_id == turn_id) and (channel is None or current.channel == channel):
            self._abort.set()
            return True
        if turn_id is not None:
            ticket = self._pending.get(turn_id)
            if ticket is not None and (channel is None or ticket.channel == channel):
                ticket.cancelled.set()
                return True
        return False

    @property
    def current_turn_id(self) -> str | None:
        return self._current.turn_id if self._current else None

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def reserve_turn(self, *, turn_id: str | None = None, channel: str | None = None) -> "TurnTicket":
        if not self.accepting:
            raise InboxCapacityError("Agent runtime is stopping")
        if len(self._pending) >= self.max_pending:
            raise InboxCapacityError("Agent pending queue is full")
        ticket = TurnTicket(turn_id=turn_id or uuid.uuid4().hex, channel=channel)
        if ticket.turn_id in self._pending or ticket.turn_id == self.current_turn_id:
            ticket.turn_id = uuid.uuid4().hex
        self._pending[ticket.turn_id] = ticket
        return ticket

    def discard(self, ticket: "TurnTicket") -> None:
        self._pending.pop(ticket.turn_id, None)

    def stop_accepting(self) -> None:
        self.accepting = False

    def cancel_pending(self) -> None:
        for ticket in self._pending.values():
            ticket.cancelled.set()

    def request_steer(self, turn_id: str, content: str, *, channel: str | None = None) -> bool:
        current = self._current
        if current is None or current.turn_id != turn_id or (channel is not None and channel != current.channel):
            return False
        self._steering.append(content)
        return True

    def take_steering(self) -> list[str]:
        items, self._steering = self._steering, []
        return items

    async def acquire_turn(self, ticket: "TurnTicket | None" = None) -> None:
        ticket = ticket or self.reserve_turn()
        acquire = asyncio.create_task(self._turn_lock.acquire())
        cancelled = asyncio.create_task(ticket.cancelled.wait())
        obtained = False
        try:
            remaining = max(0.0, self.queue_timeout - (time.monotonic() - ticket.created_at))
            done, _ = await asyncio.wait({acquire, cancelled}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            obtained = acquire in done and acquire.result()
            if ticket.cancelled.is_set():
                raise InboxCancelledError("Queued turn was cancelled")
            if not obtained:
                raise InboxQueueTimeout("Agent turn queue timed out")
        except BaseException:
            if obtained or (acquire.done() and not acquire.cancelled() and acquire.result()):
                self._turn_lock.release()
            raise
        finally:
            self.discard(ticket)
            acquire.cancel()
            cancelled.cancel()
            await asyncio.gather(acquire, cancelled, return_exceptions=True)
        self._current = ticket
        self._steering.clear()
        self._abort.clear()

    def release_turn(self) -> None:
        self._abort.clear()
        self._current = None
        self._steering.clear()
        if self._turn_lock.locked():
            self._turn_lock.release()


class InboxCapacityError(RuntimeError):
    pass


class InboxQueueTimeout(TimeoutError):
    pass


class InboxCancelledError(RuntimeError):
    pass


@dataclass
class TurnTicket:
    turn_id: str
    channel: str | None = None
    created_at: float = field(default_factory=time.monotonic)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
