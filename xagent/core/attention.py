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
    spoken_through: int = 0
    last_event_at: float = 0.0
    earliest_unattended_at: float = 0.0
    due_at: float = 0.0
    pending_addressed: bool = False
    events: List[AttentionEvent] = field(default_factory=list)
    extras: Dict[str, Any] = field(default_factory=dict)

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
                spoken_through=_safe_int(value.get("spoken_through")),
            )
        return restored

    def write(self, rooms: Dict[str, RoomState]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "rooms": {
                key: {
                    "attended_through": state.attended_through,
                    "spoken_through": state.spoken_through,
                }
                for key, state in rooms.items()
            }
        }
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(self.path)


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

    def __init__(
        self,
        *,
        agent: Any = None,
        store_path: Optional[Path] = None,
        addressed_grace: float = AgentConfig.ATTENTION_ADDRESSED_GRACE_SECONDS,
        quiet_window: float = AgentConfig.ATTENTION_QUIET_WINDOW_SECONDS,
        max_burst: int = AgentConfig.ATTENTION_MAX_BURST_MESSAGES,
        max_wait: float = AgentConfig.ATTENTION_MAX_WAIT_SECONDS,
        decide: Optional[DecideFn] = None,
    ) -> None:
        self._agent = agent
        self._store = AttentionStore(Path(store_path)) if store_path is not None else None
        self.addressed_grace = max(0.0, float(addressed_grace))
        self.quiet_window = max(0.0, float(quiet_window))
        self.max_burst = max(1, int(max_burst))
        self.max_wait = max(0.0, float(max_wait))
        self._decide = decide
        self._speakers: Dict[str, SpeakFn] = {}
        self._formatters: Dict[str, FormatFn] = {}
        self._rooms: Dict[str, RoomState] = {}
        if self._store is not None:
            self._rooms.update(self._store.read())
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        self._evaluating = False
        self._stopped = False
        self._seq = 0
        self._lock = asyncio.Lock()

    def register_speaker(self, channel: str, speaker: SpeakFn) -> None:
        self._speakers[str(channel or "").strip()] = speaker

    def set_formatter(self, channel: str, formatter: FormatFn) -> None:
        self._formatters[str(channel or "").strip()] = formatter

    def set_decider(self, decide: DecideFn) -> None:
        self._decide = decide

    def cursors(self, room_key: str) -> RoomState:
        return self._room(room_key)

    async def reset_to_present(self, storage: Any = None) -> None:
        """Skip backlog on process start. Does not run on ``notice``."""
        source = storage or getattr(self._agent, "message_storage", None)
        list_keys = getattr(source, "list_room_keys", None)
        latest_for = getattr(source, "get_latest_room_cursor", None)
        if not callable(list_keys) or not callable(latest_for):
            return
        for room_key in await list_keys():
            latest = int(await latest_for(room_key) or 0)
            state = self._room(room_key)
            state.attended_through = max(state.attended_through, latest)
            state.spoken_through = max(state.spoken_through, latest)
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
        now = time.time()
        event_time = float(timestamp) if timestamp is not None else now
        async with self._lock:
            state = self._room(key)
            assigned_cursor = int(cursor) if cursor is not None else self._next_memory_cursor(state)
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
            if extras:
                state.extras = dict(extras)
            else:
                state.extras.update(event.extras)
            if not state.events:
                state.earliest_unattended_at = now
            state.events.append(event)
            state.last_event_at = now
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
        if state.pending_count >= self.max_burst:
            return now
        if self.max_wait <= 0:
            wait_cap = now
        else:
            wait_cap = state.earliest_unattended_at + self.max_wait
        if state.pending_addressed:
            candidate = now + self.addressed_grace
        else:
            candidate = now + self.quiet_window
        return min(candidate, wait_cap)

    def _next_memory_cursor(self, state: RoomState) -> int:
        self._seq += 1
        latest_event = state.events[-1].cursor if state.events else 0
        return max(state.attended_through, latest_event, self._seq, state.spoken_through) + 1

    def _room(self, room_key: str) -> RoomState:
        state = self._rooms.get(room_key)
        if state is None:
            state = RoomState()
            self._rooms[room_key] = state
        return state

    def _armed_rooms(self) -> List[str]:
        return [key for key, state in self._rooms.items() if state.events]

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
        async with self._lock:
            state = self._room(room_key)
            batch = list(state.events)
            extras = dict(state.extras)
            through = batch[-1].cursor if batch else state.attended_through
            addressed = any(event.addressed for event in batch)
        if not batch:
            return

        self._evaluating = True
        try:
            should_reply, decision = await self._decide_batch(
                room_key,
                batch,
                extras=extras,
                addressed=addressed,
            )
            logger.info(
                "attention evaluate room=%s events=%d addressed=%s reply=%s reason=%s",
                room_key,
                len(batch),
                addressed,
                should_reply,
                (decision.reason if decision is not None else ""),
            )
            if should_reply:
                speaker = self._speakers.get(room_key_channel(room_key))
                if speaker is None:
                    logger.warning("attention has no speaker for room=%s", room_key)
                else:
                    await speaker(
                        room_key,
                        through,
                        events=batch,
                        extras=extras,
                        decision=decision,
                    )
                async with self._lock:
                    self._room(room_key).spoken_through = max(
                        self._room(room_key).spoken_through,
                        through,
                    )
            async with self._lock:
                self._advance(room_key, through)
            self._persist()
        except Exception:
            logger.exception("attention evaluate failed room=%s", room_key)
            async with self._lock:
                failed = self._room(room_key)
                if failed.events:
                    failed.due_at = time.time() + 1.0
        finally:
            self._evaluating = False

    async def _decide_batch(
        self,
        room_key: str,
        batch: List[AttentionEvent],
        *,
        extras: Dict[str, Any],
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
        context = await self._format_context(room_key, batch, extras=extras)
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

    async def _format_context(
        self,
        room_key: str,
        batch: List[AttentionEvent],
        *,
        extras: Dict[str, Any],
    ) -> str:
        formatter = self._formatters.get(room_key_channel(room_key))
        if formatter is not None:
            return await formatter(room_key, batch, extras=extras)
        return format_interval_context(
            room_key,
            batch,
            room_name=str(extras.get("room_name") or "") or None,
        )

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
