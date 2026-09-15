"""In-process world: serialize actions, fan out perceptions, run scene clock."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from . import WORLD_ACTOR_ID
from .models import EventKind, Room, WorldEvent, room_to_dict
from .scene import SceneConfig, ScheduledScene
from .store import WorldStore

SendFn = Callable[[str], Awaitable[None]]


@dataclass
class Session:
    member_id: str
    display_name: str
    connection_id: int
    send: SendFn
    rooms: set[str] = field(default_factory=set)


class World:
    """Single serialization point for one venue."""

    def __init__(self, store: WorldStore, scene: SceneConfig):
        self.store = store
        self.scene = scene
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self._connection_seq = 0
        self._started_at = time.time()
        self._scene_tasks: list[asyncio.Task[None]] = []
        self._closed = False

    @property
    def world_id(self) -> str:
        return self.scene.world_id

    def list_rooms(self) -> list[Room]:
        return self.store.list_rooms()

    async def start(self) -> None:
        for scheduled in self.scene.scenes:
            task = asyncio.create_task(self._run_scheduled_scene(scheduled))
            self._scene_tasks.append(task)

    async def close(self) -> None:
        self._closed = True
        for task in self._scene_tasks:
            task.cancel()
        if self._scene_tasks:
            await asyncio.gather(*self._scene_tasks, return_exceptions=True)
        self._scene_tasks.clear()
        self.store.close()

    async def _run_scheduled_scene(self, scheduled: ScheduledScene) -> None:
        try:
            await asyncio.sleep(scheduled.delay_seconds)
            if self._closed:
                return
            async with self._lock:
                event = self.store.append_event(
                    room_id=scheduled.room_id,
                    kind=EventKind.SCENE,
                    actor_id=WORLD_ACTOR_ID,
                    text=scheduled.text,
                )
                await self._fanout(event)
        except asyncio.CancelledError:
            return

    async def emit_scene(self, room_id: str, text: str) -> WorldEvent:
        """Test/helper hook to emit a scene event immediately."""
        async with self._lock:
            event = self.store.append_event(
                room_id=room_id,
                kind=EventKind.SCENE,
                actor_id=WORLD_ACTOR_ID,
                text=text,
            )
            await self._fanout(event)
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
        if member_id == WORLD_ACTOR_ID:
            raise ValueError("member_id 'world' is reserved")

        async with self._lock:
            connection_id = self._next_connection_id()
            previous = self._sessions.get(member_id)
            if previous is not None:
                # One body, one place: newer connection replaces the older socket.
                try:
                    await previous.send(
                        _encode(
                            "error",
                            message="replaced by a newer connection for this member_id",
                        )
                    )
                except Exception:
                    pass

            member = self.store.upsert_member(member_id, display_name)
            # Restore live subscription for rooms where this body is still present.
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
            self._sessions[member_id] = session
            return session

    async def detach(self, session: Session) -> None:
        async with self._lock:
            current = self._sessions.get(session.member_id)
            if current is None or current.connection_id != session.connection_id:
                return
            rooms = list(session.rooms)
            del self._sessions[session.member_id]
            for room_id in rooms:
                if self.store.is_present(session.member_id, room_id):
                    self.store.set_presence(session.member_id, room_id, False)
                    member = self.store.get_member(session.member_id)
                    name = member.display_name if member else session.display_name
                    event = self.store.append_event(
                        room_id=room_id,
                        kind=EventKind.LEAVE,
                        actor_id=session.member_id,
                        text=f"{name} left",
                    )
                    await self._fanout(event)

    async def handle(self, session: Session, msg_type: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            current = self._sessions.get(session.member_id)
            if current is None or current.connection_id != session.connection_id:
                await session.send(_encode("error", message="session no longer active"))
                return

            if msg_type == "join":
                await self._join(session, payload)
            elif msg_type == "leave":
                await self._leave(session, payload)
            elif msg_type == "speak":
                await self._speak(session, payload)
            elif msg_type == "sync":
                await self._sync(session, payload)
            else:
                await session.send(_encode("error", message=f"unknown type: {msg_type}"))

    async def welcome(self, session: Session) -> None:
        rooms = [room_to_dict(r) for r in self.list_rooms()]
        await session.send(
            _encode(
                "welcome",
                world_id=self.world_id,
                rooms=rooms,
                member_id=session.member_id,
                display_name=session.display_name,
            )
        )

    async def _join(self, session: Session, payload: dict[str, Any]) -> None:
        room_id = str(payload.get("room_id") or "").strip()
        room = self.store.get_room(room_id)
        if room is None:
            await session.send(_encode("error", message=f"unknown room: {room_id}"))
            return

        already = self.store.is_present(session.member_id, room_id)
        self.store.set_presence(session.member_id, room_id, True)
        session.rooms.add(room_id)

        if not already:
            event = self.store.append_event(
                room_id=room_id,
                kind=EventKind.JOIN,
                actor_id=session.member_id,
                text=f"{session.display_name} joined",
            )
            await self._fanout(event)

        present = [
            {
                "member_id": p.member_id,
                "display_name": p.display_name,
            }
            for p in self.store.list_present(room_id)
        ]
        history = [e.to_dict() for e in self.store.recent_events(room_id, limit=50)]
        await session.send(
            _encode(
                "snapshot",
                room_id=room.id,
                name=room.name,
                setting=room.setting,
                present=present,
                events=history,
            )
        )

    async def _leave(self, session: Session, payload: dict[str, Any]) -> None:
        room_id = str(payload.get("room_id") or "").strip()
        if not self.store.get_room(room_id):
            await session.send(_encode("error", message=f"unknown room: {room_id}"))
            return
        if not self.store.is_present(session.member_id, room_id):
            session.rooms.discard(room_id)
            await session.send(_encode("error", message=f"not present in room: {room_id}"))
            return

        self.store.set_presence(session.member_id, room_id, False)
        session.rooms.discard(room_id)
        event = self.store.append_event(
            room_id=room_id,
            kind=EventKind.LEAVE,
            actor_id=session.member_id,
            text=f"{session.display_name} left",
        )
        # Fanout to remaining present members (leaver already removed from presence).
        await self._fanout(event)
        # Also echo leave to the leaver so they know it stuck.
        await session.send(_encode("event", **event.to_dict()))

    async def _speak(self, session: Session, payload: dict[str, Any]) -> None:
        room_id = str(payload.get("room_id") or "").strip()
        text = str(payload.get("text") or "").strip()
        mentions_raw = payload.get("mentions") or []
        if not isinstance(mentions_raw, list):
            await session.send(_encode("error", message="mentions must be a list"))
            return
        mentions = [str(m).strip() for m in mentions_raw if str(m).strip()]

        if not text:
            await session.send(_encode("error", message="text is required"))
            return
        if not self.store.get_room(room_id):
            await session.send(_encode("error", message=f"unknown room: {room_id}"))
            return
        if not self.store.is_present(session.member_id, room_id):
            await session.send(_encode("error", message=f"not present in room: {room_id}"))
            return

        event = self.store.append_event(
            room_id=room_id,
            kind=EventKind.UTTERANCE,
            actor_id=session.member_id,
            text=text,
            mentions=mentions,
        )
        await self._fanout(event)

    async def _sync(self, session: Session, payload: dict[str, Any]) -> None:
        room_id = str(payload.get("room_id") or "").strip()
        after_seq = int(payload.get("after_seq") or 0)
        if not self.store.get_room(room_id):
            await session.send(_encode("error", message=f"unknown room: {room_id}"))
            return
        # History sync is allowed even when not present (digital group realism).
        events = [e.to_dict() for e in self.store.events_after(room_id, after_seq)]
        await session.send(
            _encode("snapshot", room_id=room_id, events=events, sync=True, after_seq=after_seq)
        )

    async def _fanout(self, event: WorldEvent) -> None:
        present_ids = {p.member_id for p in self.store.list_present(event.room_id)}
        message = _encode("event", **event.to_dict())
        dead: list[str] = []
        for member_id, session in list(self._sessions.items()):
            if member_id not in present_ids:
                continue
            if event.room_id not in session.rooms:
                # Live subscription only while this socket has joined.
                continue
            try:
                await session.send(message)
            except Exception:
                dead.append(member_id)
        for member_id in dead:
            session = self._sessions.pop(member_id, None)
            if session is None:
                continue
            for room_id in list(session.rooms):
                if self.store.is_present(member_id, room_id):
                    self.store.set_presence(member_id, room_id, False)


def _encode(msg_type: str, **payload: Any) -> str:
    from .models import encode_server_message

    return encode_server_message(msg_type, **payload)
