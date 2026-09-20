"""Per-agent attention loop: listen continuously, judge by room interval.

Perception writes the message table. This loop is the only consumer that
decides whether to speak. It keeps at most one evaluation or speech in
flight. See ``docs/listening-design.md``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ..components.memory.relationship_memory import format_speaker_label
from ..schemas import Message, ParticipationDecision
from .config import AgentConfig
from .formatters.context import RoomContextEntry, format_room_context

logger = logging.getLogger(__name__)

ROOM_KEY_METADATA_KEY = AgentConfig.ROOM_KEY_METADATA_KEY
ADDRESSED_METADATA_KEY = AgentConfig.ADDRESSED_METADATA_KEY

DecideFn = Callable[..., Awaitable[ParticipationDecision]]
SpeakFn = Callable[..., Awaitable[None]]
FormatFn = Callable[..., Awaitable[str]]
IntervalFn = Callable[..., Awaitable[None]]


class AttentionSpeakError(Exception):
    """Speaker or respond path failed to produce a delivery."""


def make_room_key(channel: str, room_id: str) -> str:
    """Stable room identity ``channel:id`` used as the attention key."""
    return f"{str(channel or '').strip()}:{str(room_id or '').strip()}"


def room_key_channel(room_key: str) -> str:
    raw = str(room_key or "")
    if ":" not in raw:
        return ""
    return raw.split(":", 1)[0]


def message_room_key(message: Message) -> str:
    return str((message.metadata or {}).get(ROOM_KEY_METADATA_KEY) or "").strip()


def message_is_addressed(message: Message) -> bool:
    return bool((message.metadata or {}).get(ADDRESSED_METADATA_KEY))


def message_storage_cursor(message: Message) -> int:
    try:
        return int((message.metadata or {}).get(AgentConfig.MESSAGE_STORAGE_CURSOR_KEY) or 0)
    except (TypeError, ValueError):
        return 0


def reply_anchor_event(batch: List["AttentionEvent"]) -> Optional["AttentionEvent"]:
    """Last addressed event in the batch, else the last event."""
    for event in reversed(batch):
        if event.addressed:
            return event
    return batch[-1] if batch else None


@dataclass
class AttentionEvent:
    """One inbound utterance the loop has been asked to consider."""

    room_key: str
    cursor: int
    addressed: bool
    content: str
    sender_id: str = ""
    sender_name: str = ""
    timestamp: float = 0.0
    extras: Dict[str, Any] = field(default_factory=dict)

    def to_entry(self) -> RoomContextEntry:
        occurred = datetime.fromtimestamp(self.timestamp or time.time(), tz=timezone.utc)
        label = format_speaker_label(self.sender_id, self.sender_name) or self.sender_id or "someone"
        return RoomContextEntry(
            speaker_label=label,
            occurred_at=occurred.replace(tzinfo=None),
            text=self.content,
            is_self=False,
        )


@dataclass
class RoomState:
    attended_through: int = 0
    last_event_at: float = 0.0
    earliest_unattended_at: float = 0.0
    due_at: float = 0.0
    pending_addressed: bool = False
    events: List[AttentionEvent] = field(default_factory=list)
    speak_failures: int = 0
    retry_at: float = 0.0

    @property
    def pending_count(self) -> int:
        return len(self.events)


class AttentionStore:
    """File-backed per-room cursors under the messages directory."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def read(self) -> Dict[str, RoomState]:
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            logger.warning("Failed to read attention cursors: %s", exc)
            return {}
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("Invalid attention cursor JSON: %s", exc)
            return {}
        rooms = payload.get("rooms") if isinstance(payload, dict) else None
        if not isinstance(rooms, dict):
            return {}
        restored: Dict[str, RoomState] = {}
        for key, value in rooms.items():
            if not isinstance(value, dict):
                continue
            restored[str(key)] = RoomState(
                attended_through=_safe_int(value.get("attended_through")),
            )
        return restored

    def write(self, rooms: Dict[str, RoomState]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "rooms": {
                key: {"attended_through": state.attended_through}
                for key, state in rooms.items()
            }
        }
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(self.path)


async def build_room_context_from_storage(
    storage: Any,
    room_key: str,
    *,
    start_exclusive: int,
    end_inclusive: int,
    limit: int = AgentConfig.ATTENTION_HISTORY_MESSAGES,
    room_name: Optional[str] = None,
) -> str:
    """Load recent rows for *room_key* from the message table as room context."""
    get_for_room = getattr(storage, "get_messages_for_room", None)
    if not callable(get_for_room):
        return ""
    messages = await get_for_room(
        room_key,
        start_exclusive=start_exclusive,
        end_inclusive=end_inclusive,
        limit=limit,
    )
    if not messages:
        return ""
    events = events_from_messages(messages, room_key)
    resolved_room_name = room_name or next(
        (msg.room_name for msg in messages if msg.room_name),
        None,
    )
    return format_interval_context(
        room_key,
        events,
        room_name=resolved_room_name,
    )


def format_interval_context(
    room_key: str,
    events: List[AttentionEvent],
    *,
    room_name: Optional[str] = None,
    history: Optional[List[AttentionEvent]] = None,
) -> str:
    """Render unattended events (and optional earlier history) as room context."""
    room_id = room_key.split(":", 1)[1] if ":" in room_key else room_key
    combined = [*(history or []), *events]
    entries = [event.to_entry() for event in combined]
    return format_room_context(room_id, entries, room_name=room_name)


class AttentionLoop:
    """One serial consumer per agent that evaluates unattended room intervals."""

    _IDLE_ROOM_TTL_SECONDS = 3600.0

    def __init__(
        self,
        *,
        agent: Any = None,
        store_path: Optional[Path] = None,
        quiet_window: float = AgentConfig.ATTENTION_QUIET_WINDOW_SECONDS,
        max_wait: float = AgentConfig.ATTENTION_MAX_WAIT_SECONDS,
        decide: Optional[DecideFn] = None,
        on_interval: Optional[IntervalFn] = None,
    ) -> None:
        self._agent = agent
        self._store = AttentionStore(Path(store_path)) if store_path is not None else None
        self.quiet_window = max(0.0, float(quiet_window))
        self.max_wait = max(0.0, float(max_wait))
        self._decide = decide
        self._on_interval = on_interval
        self._speakers: Dict[str, SpeakFn] = {}
        self._rooms: Dict[str, RoomState] = {}
        if self._store is not None:
            self._rooms.update(self._store.read())
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        self._evaluating = False
        self._stopped = False
        self._lock = asyncio.Lock()

    def register_speaker(self, channel: str, speaker: SpeakFn) -> None:
        self._speakers[str(channel or "").strip()] = speaker

    def set_decider(self, decide: DecideFn) -> None:
        self._decide = decide

    def cursors(self, room_key: str) -> RoomState:
        return self._room(room_key)

    async def reset_to_present(self, storage: Any = None) -> None:
        """Skip ambient backlog on start; replay recent addressed direct requests."""
        source = storage or getattr(self._agent, "message_storage", None)
        list_keys = getattr(source, "list_room_keys", None)
        latest_for = getattr(source, "get_latest_room_cursor", None)
        get_for_room = getattr(source, "get_messages_for_room", None)
        if not callable(list_keys) or not callable(latest_for):
            return
        replay_window = float(AgentConfig.ATTENTION_REPLAY_ADDRESSED_SECONDS)
        now = time.time()
        for room_key in await list_keys():
            latest = int(await latest_for(room_key) or 0)
            state = self._room(room_key)
            if not callable(get_for_room):
                state.attended_through = max(state.attended_through, latest)
                state.events.clear()
                continue
            pending = await get_for_room(
                room_key,
                start_exclusive=state.attended_through,
                end_inclusive=latest,
            )
            addressed_recent = [
                message
                for message in pending
                if message_is_addressed(message)
                and float(message.timestamp or 0.0) >= now - replay_window
            ]
            if addressed_recent:
                first_ts = min(float(message.timestamp or 0.0) for message in addressed_recent)
                replay_messages = [
                    message
                    for message in pending
                    if float(message.timestamp or 0.0) >= first_ts
                ]
                state.events.clear()
                state.pending_addressed = False
                state.due_at = 0.0
                for message in replay_messages:
                    await self.notice(
                        room_key,
                        addressed=message_is_addressed(message),
                        content=str(message.content or ""),
                        cursor=message_storage_cursor(message),
                        sender_id=str(message.sender_id or ""),
                        sender_name=str((message.metadata or {}).get("sender_name") or ""),
                        timestamp=float(message.timestamp or 0.0),
                        extras={
                            key: value
                            for key, value in (message.metadata or {}).items()
                            if key not in {ROOM_KEY_METADATA_KEY, ADDRESSED_METADATA_KEY}
                        },
                    )
                continue
            state.attended_through = max(state.attended_through, latest)
            state.events.clear()
            state.pending_addressed = False
            state.due_at = 0.0
        self._persist()

    async def stop(self) -> None:
        self._stopped = True
        self._wake.set()
        task = self._task
        if task is None:
            self._stopped = False
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._task = None
        self._stopped = False
        self._wake = asyncio.Event()

    async def idle(self, timeout: float = 5.0) -> None:
        """Wait until nothing is armed or evaluating. Used by tests."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            if not self._evaluating and not self._armed_rooms():
                await asyncio.sleep(0)
                if not self._evaluating and not self._armed_rooms():
                    return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError("attention loop did not become idle")
            await asyncio.sleep(min(0.02, remaining))

    async def notice(
        self,
        room_key: str,
        *,
        addressed: bool,
        content: str,
        cursor: Optional[int] = None,
        sender_id: str = "",
        sender_name: str = "",
        timestamp: Optional[float] = None,
        extras: Optional[Dict[str, Any]] = None,
    ) -> AttentionEvent:
        """Record that *room_key* has a new unattended utterance."""
        key = str(room_key or "").strip()
        if not key:
            raise ValueError("room_key is required")
        if cursor is None or int(cursor) <= 0:
            logger.warning(
                "attention notice missing storage cursor room=%s addressed=%s",
                key,
                addressed,
            )
            raise ValueError("storage cursor is required")
        assigned_cursor = int(cursor)
        now = time.time()
        event_time = float(timestamp) if timestamp is not None else now
        async with self._lock:
            state = self._room(key)
            event = AttentionEvent(
                room_key=key,
                cursor=assigned_cursor,
                addressed=bool(addressed),
                content=str(content or ""),
                sender_id=str(sender_id or ""),
                sender_name=str(sender_name or ""),
                timestamp=event_time,
                extras=dict(extras or {}),
            )
            if not state.events:
                state.earliest_unattended_at = now
            state.events.append(event)
            state.last_event_at = now
            state.speak_failures = 0
            state.retry_at = 0.0
            if event.addressed:
                state.pending_addressed = True
            state.due_at = self._due_at(state, now=now)
        logger.info(
            "attention notice room=%s cursor=%s addressed=%s pending=%d due_in=%.2fs",
            key,
            event.cursor,
            event.addressed,
            state.pending_count,
            max(0.0, state.due_at - time.time()),
        )
        self._ensure_loop()
        self._wake.set()
        return event

    def _due_at(self, state: RoomState, *, now: float) -> float:
        if not state.events:
            return 0.0
        if state.retry_at > now:
            return state.retry_at
        if self.max_wait <= 0:
            wait_cap = now
        else:
            wait_cap = state.earliest_unattended_at + self.max_wait
        if state.pending_addressed:
            candidate = now
        else:
            candidate = now + self.quiet_window
        return min(candidate, wait_cap)

    def _room(self, room_key: str) -> RoomState:
        state = self._rooms.get(room_key)
        if state is None:
            state = RoomState()
            self._rooms[room_key] = state
        return state

    def _armed_rooms(self) -> List[str]:
        now = time.time()
        armed: List[str] = []
        for key, state in list(self._rooms.items()):
            if state.events:
                armed.append(key)
                continue
            if (
                not state.events
                and state.last_event_at > 0
                and now - state.last_event_at > self._IDLE_ROOM_TTL_SECONDS
            ):
                del self._rooms[key]
        return armed

    def _ensure_loop(self) -> None:
        if self._stopped:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._task is None or self._task.done():
            self._wake = asyncio.Event()
            self._task = loop.create_task(self._run(), name="xagent-attention")

    async def _run(self) -> None:
        try:
            while not self._stopped:
                due_key, delay = self._next_due()
                if due_key is None:
                    self._wake.clear()
                    if self._armed_rooms():
                        await asyncio.sleep(0.01)
                        continue
                    await self._wake.wait()
                    continue
                if delay > 0:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        pass
                    continue
                await self._evaluate(due_key)
        except asyncio.CancelledError:
            raise

    def _next_due(self) -> tuple[Optional[str], float]:
        now = time.time()
        best_key: Optional[str] = None
        best_due = 0.0
        best_addressed = False
        best_age = 0.0
        for key, state in self._rooms.items():
            if not state.events or state.due_at <= 0:
                continue
            age = now - state.earliest_unattended_at
            if best_key is None:
                best_key = key
                best_due = state.due_at
                best_addressed = state.pending_addressed
                best_age = age
                continue
            if state.pending_addressed and not best_addressed:
                best_key = key
                best_due = state.due_at
                best_addressed = True
                best_age = age
                continue
            if state.pending_addressed == best_addressed and (
                state.due_at < best_due or (state.due_at == best_due and age > best_age)
            ):
                best_key = key
                best_due = state.due_at
                best_addressed = state.pending_addressed
                best_age = age
        if best_key is None:
            return None, 0.0
        return best_key, max(0.0, best_due - now)

    async def _evaluate(self, room_key: str) -> None:
        started = time.monotonic()
        async with self._lock:
            state = self._room(room_key)
            batch = list(state.events)
            through = batch[-1].cursor if batch else state.attended_through
            addressed = any(event.addressed for event in batch)
        if not batch:
            return

        self._evaluating = True
        should_reply = False
        decision: Optional[ParticipationDecision] = None
        spoke = False
        try:
            should_reply, decision = await self._decide_batch(
                room_key,
                batch,
                addressed=addressed,
            )
            if should_reply:
                speaker = self._speakers.get(room_key_channel(room_key))
                if speaker is None:
                    raise AttentionSpeakError(f"no speaker for {room_key}")
                anchor = reply_anchor_event(batch)
                merged_extras = dict(anchor.extras if anchor else {})
                merged_extras["reply_message_id"] = (anchor.extras or {}).get("message_id") if anchor else None
                await speaker(
                    room_key,
                    through,
                    events=batch,
                    extras=merged_extras,
                    decision=decision,
                )
                spoke = True
            await self._emit_interval(room_key, batch, addressed, should_reply, decision)
            async with self._lock:
                self._room(room_key).speak_failures = 0
                self._room(room_key).retry_at = 0.0
                self._advance(room_key, through)
            self._persist()
            logger.info(
                "attention evaluate room=%s events=%d addressed=%s reply=%s reason=%s latency_ms=%.0f",
                room_key,
                len(batch),
                addressed,
                should_reply,
                (decision.reason if decision is not None else ""),
                (time.monotonic() - started) * 1000,
            )
        except AttentionSpeakError:
            await self._handle_speak_failure(room_key, batch, addressed)
        except Exception:
            logger.exception("attention evaluate failed room=%s", room_key)
            await self._handle_speak_failure(room_key, batch, addressed)
        finally:
            self._evaluating = False

    async def _handle_speak_failure(
        self,
        room_key: str,
        batch: List[AttentionEvent],
        addressed: bool,
    ) -> None:
        max_failures = int(AgentConfig.ATTENTION_SPEAK_MAX_FAILURES)
        async with self._lock:
            state = self._room(room_key)
            state.speak_failures += 1
            failures = state.speak_failures
            if failures >= max_failures:
                through = batch[-1].cursor if batch else state.attended_through
                logger.warning(
                    "attention giving up room=%s after %d failures; advancing through=%s addressed=%s",
                    room_key,
                    failures,
                    through,
                    addressed,
                )
                state.speak_failures = 0
                state.retry_at = 0.0
                self._advance(room_key, through)
                self._persist()
                return
            delay = min(32.0, 2 ** max(0, failures - 1))
            state.retry_at = time.time() + delay
            state.due_at = state.retry_at
        logger.warning(
            "attention speak failed room=%s attempt=%d retry_in=%.1fs addressed=%s",
            room_key,
            failures,
            delay,
            addressed,
        )

    async def _emit_interval(
        self,
        room_key: str,
        batch: List[AttentionEvent],
        addressed: bool,
        should_reply: bool,
        decision: Optional[ParticipationDecision],
    ) -> None:
        callback = self._on_interval
        if callback is None:
            agent = self._agent
            callback = getattr(agent, "on_attention_interval", None) if agent is not None else None
        if not callable(callback):
            return
        await callback(
            room_key=room_key,
            events=batch,
            addressed=addressed,
            should_reply=should_reply,
            decision=decision,
        )

    async def _decide_batch(
        self,
        room_key: str,
        batch: List[AttentionEvent],
        *,
        addressed: bool,
    ) -> tuple[bool, Optional[ParticipationDecision]]:
        if addressed:
            return True, ParticipationDecision(
                should_reply=True,
                reason="addressed to agent",
                addressing=[str(event.cursor) for event in batch if event.addressed],
            )
        decider = self._decide
        if decider is None:
            agent = self._agent
            maybe = getattr(agent, "decide_participation", None) if agent is not None else None
            decider = maybe if callable(maybe) else None
        if decider is None:
            return False, ParticipationDecision(should_reply=False, reason="no decider")
        storage = getattr(self._agent, "message_storage", None)
        attended = self._room(room_key).attended_through
        through = batch[-1].cursor
        context = await build_room_context_from_storage(
            storage,
            room_key,
            start_exclusive=attended,
            end_inclusive=through,
            room_name=str((batch[-1].extras or {}).get("room_name") or "") or None,
        )
        if not context.strip():
            context = format_interval_context(
                room_key,
                batch,
                room_name=str((batch[-1].extras or {}).get("room_name") or "") or None,
            )
        extras = batch[-1].extras if batch else {}
        decision = await decider(
            context=context,
            source=str(extras.get("source") or room_key_channel(room_key) or "environment"),
            event_type=str(extras.get("event_type") or "room_interval"),
            metadata={
                "room_key": room_key,
                "event_count": len(batch),
                **{key: value for key, value in extras.items() if key != "raw_msg"},
            },
        )
        parsed = _coerce_decision(decision)
        return bool(parsed.should_reply), parsed

    def _advance(self, room_key: str, through: int) -> None:
        state = self._room(room_key)
        state.attended_through = max(state.attended_through, through)
        state.events = [event for event in state.events if event.cursor > through]
        state.pending_addressed = any(event.addressed for event in state.events)
        if state.events:
            now = time.time()
            state.earliest_unattended_at = now
            state.due_at = self._due_at(state, now=now)
        else:
            state.due_at = 0.0
            state.pending_addressed = False

    def _persist(self) -> None:
        if self._store is None:
            return
        try:
            self._store.write(self._rooms)
        except OSError as exc:
            logger.warning("Failed to persist attention cursors: %s", exc)


def events_from_messages(messages: List[Message], room_key: str) -> List[AttentionEvent]:
    events: List[AttentionEvent] = []
    for message in messages:
        events.append(
            AttentionEvent(
                room_key=message_room_key(message) or room_key,
                cursor=message_storage_cursor(message),
                addressed=message_is_addressed(message),
                content=str(message.content or ""),
                sender_id=str(message.sender_id or ""),
                sender_name=str((message.metadata or {}).get("sender_name") or ""),
                timestamp=float(message.timestamp or 0.0),
                extras=dict(message.metadata or {}),
            )
        )
    return events


def _coerce_decision(decision: Any) -> ParticipationDecision:
    if isinstance(decision, ParticipationDecision):
        return decision
    if isinstance(decision, dict):
        addressing = decision.get("addressing") or []
        if not isinstance(addressing, list):
            addressing = [str(addressing)]
        return ParticipationDecision(
            should_reply=bool(decision.get("should_reply")),
            reason=str(decision.get("reason") or "").strip() or None,
            addressing=[str(item) for item in addressing if str(item).strip()],
        )
    return ParticipationDecision(
        should_reply=bool(getattr(decision, "should_reply", False)),
        reason=str(getattr(decision, "reason", None) or "").strip() or None,
        addressing=[
            str(item)
            for item in list(getattr(decision, "addressing", None) or [])
            if str(item).strip()
        ],
    )


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
