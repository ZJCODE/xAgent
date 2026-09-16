"""In-process world: serialize actions, fan out perceptions, run scene clock."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from . import (
    MAX_DISPLAY_NAME_LENGTH,
    MAX_MENTIONS,
    MAX_TEXT_LENGTH,
    OUTBOUND_QUEUE_SIZE,
    PROTOCOL_VERSION,
    SNAPSHOT_HISTORY_LIMIT,
    SPEAK_RATE_PER_SEC,
    SYNC_PAGE_SIZE,
    WORLD_ACTOR_ID,
)
from .clock import Clock, default_clock
from .models import (
    EventKind,
    Room,
    WorldEvent,
    encode_error,
    encode_server_message,
    member_to_dict,
    room_to_dict,
)
from .paths import validate_member_id, validate_room_id
from .scene import SceneConfig, ScheduledScene
from .store import WorldStore

logger = logging.getLogger(__name__)

SendFn = Callable[[str], Awaitable[None]]


@dataclass
class Session:
    member_id: str
    display_name: str
    connection_id: int
    send: SendFn
    rooms: set[str] = field(default_factory=set)
    outbound: asyncio.Queue[Optional[str]] = field(default_factory=lambda: asyncio.Queue(maxsize=OUTBOUND_QUEUE_SIZE))
    replaced: asyncio.Event = field(default_factory=asyncio.Event)
    lagged_rooms: set[str] = field(default_factory=set)
    pump_task: Optional[asyncio.Task[None]] = None


class World:
    """Single serialization point for one venue."""

    def __init__(self, store: WorldStore, scene: SceneConfig, *, clock: Optional[Clock] = None):
        self.store = store
        self.scene = scene
        self.clock = default_clock(clock)
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self._connection_seq = 0
        self._scene_tasks: list[asyncio.Task[None]] = []
        self._closed = False
        self._speak_times: dict[str, list[float]] = {}

    @property
    def world_id(self) -> str:
        return self.scene.world_id

    def list_rooms(self) -> list[Room]:
        return self.store.list_rooms()

    async def start(self) -> None:
        epoch = self.store.ensure_world_epoch()
        now = self.clock.now()
        for scheduled in self.scene.scenes:
            task = asyncio.create_task(
                self._run_scheduled_scene(scheduled, epoch=epoch, now=now),
                name=f"scene-{scheduled.fire_key()}",
            )
            self._scene_tasks.append(task)

    async def close(self) -> None:
        if self._closed:
            try:
                self.store.close()
            except Exception:
                pass
            return
        self._closed = True
        for task in self._scene_tasks:
            task.cancel()
        pumps = [s.pump_task for s in list(self._sessions.values()) if s.pump_task is not None]
        for task in pumps:
            task.cancel()
        pending = [t for t in (*self._scene_tasks, *pumps) if t is not None]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._scene_tasks.clear()
        self._sessions.clear()
        try:
            self.store.close()
        except Exception:
            pass

    async def wait_idle(self, *sessions: Session) -> None:
        """Test helper: wait until outbound queues have been drained."""
        targets = list(sessions) if sessions else list(self._sessions.values())
        if not targets:
            await asyncio.sleep(0)
            return
        await asyncio.gather(*(session.outbound.join() for session in targets))

    async def _run_scheduled_scene(
        self,
        scheduled: ScheduledScene,
        *,
        epoch: float,
        now: float,
    ) -> None:
        key = scheduled.fire_key()
        try:
            if self.store.scene_already_fired(key):
                return
            fire_at = epoch + scheduled.delay_seconds
            remaining = fire_at - now
            if remaining > 0:
                await self.clock.sleep(remaining)
            if self._closed:
                return
            async with self._lock:
                if self.store.scene_already_fired(key):
                    return
                event = self.store.append_event(
                    room_id=scheduled.room_id,
                    kind=EventKind.SCENE,
                    actor_id=WORLD_ACTOR_ID,
                    text=scheduled.text,
                )
                self.store.mark_scene_fired(key, seq=event.seq)
                self._fanout(event)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("scheduled scene %s failed", key)

    async def emit_scene(self, room_id: str, text: str) -> WorldEvent:
        """Test/helper hook to emit a scene event immediately."""
        async with self._lock:
            event = self.store.append_event(
                room_id=room_id,
                kind=EventKind.SCENE,
                actor_id=WORLD_ACTOR_ID,
                text=text,
            )
            self._fanout(event)
            return event

    def _next_connection_id(self) -> int:
        self._connection_seq += 1
        return self._connection_seq

    async def attach(
        self,
        *,
        member_id: str,
        display_name: str,
        send: SendFn,
    ) -> Session:
        member_id = member_id.strip()
        display_name = (display_name or member_id).strip()
        if not member_id:
            raise ValueError("member_id is required")
        try:
            member_id = validate_member_id(member_id)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if member_id == WORLD_ACTOR_ID:
            raise ValueError("member_id 'world' is reserved")
        if len(display_name) > MAX_DISPLAY_NAME_LENGTH:
            display_name = display_name[:MAX_DISPLAY_NAME_LENGTH].rstrip()
        display_name = "".join(ch for ch in display_name if ch.isprintable()) or member_id

        async with self._lock:
            connection_id = self._next_connection_id()
            previous = self._sessions.get(member_id)
            if previous is not None:
                # One body, one place: newer connection replaces the older socket.
                self._enqueue(
                    previous,
                    encode_error(
                        "replaced",
                        "replaced by a newer connection for this member_id",
                    ),
                )
                previous.replaced.set()
                self._stop_pump(previous)

            member = self.store.upsert_member(member_id, display_name)
            restored_rooms = {
                room.id
                for room in self.store.list_rooms()
                if self.store.is_present(member_id, room.id)
            }
            session = Session(
                member_id=member.id,
                display_name=member.display_name,
                connection_id=connection_id,
                send=send,
                rooms=restored_rooms,
            )
            session.pump_task = asyncio.create_task(
                self._pump(session),
                name=f"pump-{member_id}-{connection_id}",
            )
            self._sessions[member_id] = session
            return session

    async def detach(self, session: Session) -> None:
        async with self._lock:
            self._detach_locked(session)

    async def reap_dead(self, session: Session) -> None:
        async with self._lock:
            current = self._sessions.get(session.member_id)
            if current is None or current.connection_id != session.connection_id:
                return
            self._detach_locked(session)

    def _detach_locked(self, session: Session) -> None:
        current = self._sessions.get(session.member_id)
        if current is None or current.connection_id != session.connection_id:
            return
        rooms = list(session.rooms)
        del self._sessions[session.member_id]
        session.replaced.set()
        if session.pump_task is not asyncio.current_task():
            self._stop_pump(session)
        for room_id in rooms:
            if not self.store.is_present(session.member_id, room_id):
                continue
            with self.store.transaction():
                self.store.set_presence(session.member_id, room_id, False)
                event = self.store.append_event(
                    room_id=room_id,
                    kind=EventKind.LEAVE,
                    actor_id=session.member_id,
                    text="",
                )
            self._fanout(event)

    def _stop_pump(self, session: Session) -> None:
        # Sentinel lets the pump flush pending frames (e.g. "replaced") then exit.
        # Do not cancel here: cancellation would drop the last outbound messages.
        self._enqueue(session, None)

    def push_error(self, session: Session, code: str, message: str, **extra: Any) -> None:
        self._enqueue(session, encode_error(code, message, **extra))

    async def _pump(self, session: Session) -> None:
        try:
            while True:
                raw = await session.outbound.get()
                try:
                    if raw is None:
                        return
                    await session.send(raw)
                finally:
                    session.outbound.task_done()
        except asyncio.CancelledError:
            self._drain_queue(session)
            raise
        except Exception:
            logger.debug("session %s pump failed", session.member_id, exc_info=True)
            await self.reap_dead(session)

    def _drain_queue(self, session: Session) -> None:
        while True:
            try:
                session.outbound.get_nowait()
                session.outbound.task_done()
            except asyncio.QueueEmpty:
                return

    def _enqueue(self, session: Session, message: Optional[str]) -> bool:
        try:
            session.outbound.put_nowait(message)
            return True
        except asyncio.QueueFull:
            return False

    def _enqueue_or_lag(self, session: Session, message: str, event: WorldEvent) -> None:
        if event.room_id in session.lagged_rooms:
            return
        if self._enqueue(session, message):
            return
        session.lagged_rooms.add(event.room_id)
        self._drain_queue(session)
        lagged = encode_server_message(
            "lagged",
            room_id=event.room_id,
            after_seq=max(0, event.seq - 1),
        )
        self._enqueue(session, lagged)

    async def handle(self, session: Session, msg_type: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            current = self._sessions.get(session.member_id)
            if current is None or current.connection_id != session.connection_id:
                self._enqueue(session, encode_error("session_inactive", "session no longer active"))
                return

            if msg_type == "join":
                self._join(session, payload)
            elif msg_type == "leave":
                self._leave(session, payload)
            elif msg_type == "speak":
                self._speak(session, payload)
            elif msg_type == "sync":
                self._sync(session, payload)
            else:
                self._enqueue(session, encode_error("unknown_type", f"unknown type: {msg_type}"))

    async def welcome(self, session: Session) -> None:
        heads = self.store.room_heads()
        rooms = []
        for room in self.list_rooms():
            body = room_to_dict(room)
            body.update(heads.get(room.id, {"latest_seq": 0, "latest_room_seq": 0}))
            rooms.append(body)
        self._enqueue(
            session,
            encode_server_message(
                "welcome",
                protocol_version=PROTOCOL_VERSION,
                world_id=self.world_id,
                rooms=rooms,
                member_id=session.member_id,
                display_name=session.display_name,
                present_rooms=sorted(session.rooms),
            ),
        )

    def _parse_room_id(self, payload: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
        raw = str(payload.get("room_id") or "").strip()
        if not raw:
            return None, encode_error("bad_payload", "room_id is required")
        try:
            return validate_room_id(raw), None
        except ValueError as exc:
            return None, encode_error("bad_payload", str(exc))

    def _join(self, session: Session, payload: dict[str, Any]) -> None:
        room_id, err = self._parse_room_id(payload)
        if err:
            self._enqueue(session, err)
            return
        assert room_id is not None
        room = self.store.get_room(room_id)
        if room is None:
            self._enqueue(session, encode_error("unknown_room", f"unknown room: {room_id}"))
            return

        already = self.store.is_present(session.member_id, room_id)
        with self.store.transaction():
            self.store.set_presence(session.member_id, room_id, True)
            event = None
            if not already:
                event = self.store.append_event(
                    room_id=room_id,
                    kind=EventKind.JOIN,
                    actor_id=session.member_id,
                    text="",
                )
        session.rooms.add(room_id)
        session.lagged_rooms.discard(room_id)
        if event is not None:
            self._fanout(event, exclude={session.member_id})

        present = [member_to_dict_from_presence(p) for p in self.store.list_present(room_id)]
        history = [e.to_dict() for e in self.store.recent_events(room_id, limit=SNAPSHOT_HISTORY_LIMIT)]
        self._enqueue(
            session,
            encode_server_message(
                "snapshot",
                room_id=room.id,
                name=room.name,
                setting=room.setting,
                present=present,
                events=history,
            ),
        )

    def _leave(self, session: Session, payload: dict[str, Any]) -> None:
        room_id, err = self._parse_room_id(payload)
        if err:
            self._enqueue(session, err)
            return
        assert room_id is not None
        if not self.store.get_room(room_id):
            self._enqueue(session, encode_error("unknown_room", f"unknown room: {room_id}"))
            return
        if not self.store.is_present(session.member_id, room_id):
            session.rooms.discard(room_id)
            self._enqueue(session, encode_error("not_present", f"not present in room: {room_id}"))
            return

        with self.store.transaction():
            self.store.set_presence(session.member_id, room_id, False)
            event = self.store.append_event(
                room_id=room_id,
                kind=EventKind.LEAVE,
                actor_id=session.member_id,
                text="",
            )
        session.rooms.discard(room_id)
        self._fanout(event)
        self._enqueue(session, encode_server_message("event", **event.to_dict()))

    def _speak(self, session: Session, payload: dict[str, Any]) -> None:
        room_id, err = self._parse_room_id(payload)
        if err:
            self._enqueue(session, err)
            return
        assert room_id is not None
        text = str(payload.get("text") or "")
        if not text.strip():
            self._enqueue(session, encode_error("text_required", "text is required"))
            return
        if len(text) > MAX_TEXT_LENGTH:
            self._enqueue(
                session,
                encode_error(
                    "text_too_long",
                    f"text exceeds {MAX_TEXT_LENGTH} characters",
                ),
            )
            return
        mentions_raw = payload.get("mentions") or []
        if not isinstance(mentions_raw, list):
            self._enqueue(session, encode_error("bad_payload", "mentions must be a list"))
            return
        mentions = [str(m).strip() for m in mentions_raw if str(m).strip()]
        if len(mentions) > MAX_MENTIONS:
            self._enqueue(
                session,
                encode_error("bad_payload", f"mentions exceeds {MAX_MENTIONS} entries"),
            )
            return

        if not self.store.get_room(room_id):
            self._enqueue(session, encode_error("unknown_room", f"unknown room: {room_id}"))
            return
        if not self.store.is_present(session.member_id, room_id):
            self._enqueue(session, encode_error("not_present", f"not present in room: {room_id}"))
            return
        if not self._allow_speak(session.member_id):
            self._enqueue(session, encode_error("rate_limited", "speak rate exceeded"))
            return

        event = self.store.append_event(
            room_id=room_id,
            kind=EventKind.UTTERANCE,
            actor_id=session.member_id,
            text=text.strip(),
            mentions=mentions,
        )
        self._fanout(event)

    def _allow_speak(self, member_id: str) -> bool:
        now = self.clock.now()
        window = self._speak_times.setdefault(member_id, [])
        window[:] = [t for t in window if now - t < 1.0]
        if len(window) >= SPEAK_RATE_PER_SEC:
            return False
        window.append(now)
        return True

    def _sync(self, session: Session, payload: dict[str, Any]) -> None:
        room_id, err = self._parse_room_id(payload)
        if err:
            self._enqueue(session, err)
            return
        assert room_id is not None
        raw_seq = payload.get("after_seq") if "after_seq" in payload else 0
        try:
            after_seq = int(raw_seq or 0)
        except (TypeError, ValueError):
            self._enqueue(session, encode_error("bad_payload", "after_seq must be an integer"))
            return
        if after_seq < 0:
            self._enqueue(session, encode_error("bad_payload", "after_seq must be >= 0"))
            return
        if not self.store.get_room(room_id):
            self._enqueue(session, encode_error("unknown_room", f"unknown room: {room_id}"))
            return

        page_limit = SYNC_PAGE_SIZE
        fetched = self.store.events_after(room_id, after_seq, limit=page_limit + 1)
        has_more = len(fetched) > page_limit
        events = fetched[:page_limit]
        next_after_seq = events[-1].seq if events else after_seq
        session.lagged_rooms.discard(room_id)
        self._enqueue(
            session,
            encode_server_message(
                "snapshot",
                room_id=room_id,
                events=[e.to_dict() for e in events],
                sync=True,
                after_seq=after_seq,
                has_more=has_more,
                next_after_seq=next_after_seq,
            ),
        )

    def _fanout(self, event: WorldEvent, *, exclude: Optional[set[str]] = None) -> None:
        present_ids = {p.member_id for p in self.store.list_present(event.room_id)}
        message = encode_server_message("event", **event.to_dict())
        skip = exclude or set()
        for member_id, session in list(self._sessions.items()):
            if member_id in skip:
                continue
            if member_id not in present_ids:
                continue
            if event.room_id not in session.rooms:
                continue
            self._enqueue_or_lag(session, message, event)


def member_to_dict_from_presence(record: Any) -> dict[str, Any]:
    from .models import PresenceRecord

    if isinstance(record, PresenceRecord):
        return {
            "member_id": record.member_id,
            "display_name": record.display_name,
        }
    return member_to_dict(record)
