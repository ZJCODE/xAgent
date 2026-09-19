"""In-process world: serialize actions and fan out perceptions."""

from __future__ import annotations

import asyncio
import base64
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from . import (
    MAX_ATTACHMENTS,
    PROTOCOL_CAPABILITIES,
    MAX_ATTACHMENTS_BYTES,
    MAX_DISPLAY_NAME_LENGTH,
    MAX_FILE_BYTES,
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
from .config import WorldConfig
from .models import (
    Attachment,
    EventKind,
    WorldEvent,
    encode_error,
    encode_server_message,
    normalize_member_kind,
)
from .paths import validate_member_id
from .store import WorldStore

logger = logging.getLogger(__name__)

SendFn = Callable[[str], Awaitable[None]]


class MemberTaken(ValueError):
    """Another live connection already embodies this member_id and no valid resume_token was shown."""

    def __init__(self, member_id: str):
        super().__init__(f"member_id is present from another connection: {member_id}")
        self.member_id = member_id


@dataclass
class Session:
    member_id: str
    display_name: str
    connection_id: int
    send: SendFn
    member_kind: str = "human"
    present: bool = False
    # Proof of "same body": a later hello for this member_id may replace this
    # socket only when it presents this token.
    resume_token: str = ""
    outbound: asyncio.Queue[Optional[str]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=OUTBOUND_QUEUE_SIZE)
    )
    replaced: asyncio.Event = field(default_factory=asyncio.Event)
    lagged: bool = False
    pump_task: Optional[asyncio.Task[None]] = None


class World:
    """Single serialization point for one venue."""

    def __init__(self, store: WorldStore, config: WorldConfig, *, clock: Optional[Clock] = None):
        self.store = store
        self.config = config
        self.clock = default_clock(clock)
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self._connection_seq = 0
        self._closed = False
        self._speak_times: dict[str, list[float]] = {}

    @property
    def world_id(self) -> str:
        return self.config.world_id

    async def start(self) -> None:
        self.store.ensure_world_epoch()
        self.store.clear_presence()

    def list_live_present(self) -> list[dict[str, str]]:
        rows = [
            {
                "member_id": session.member_id,
                "display_name": session.display_name,
                "kind": normalize_member_kind(session.member_kind),
            }
            for session in self._sessions.values()
            if session.present
        ]
        rows.sort(key=lambda item: (item["display_name"].lower(), item["member_id"]))
        return rows

    @staticmethod
    def present_by_kind(present: list[dict[str, Any]]) -> dict[str, int]:
        counts = {"human": 0, "agent": 0, "script": 0}
        for item in present:
            key = normalize_member_kind(item.get("kind"))
            counts[key] = counts.get(key, 0) + 1
        return counts

    def disconnect_all(self, *, code: str = "world_gone", message: str = "world deleted") -> None:
        """Kick every connected session. Does not close the store."""
        for session in list(self._sessions.values()):
            self.push_error(session, code, message)
            session.replaced.set()

    async def close(self) -> None:
        if self._closed:
            try:
                self.store.close()
            except Exception:
                pass
            return
        self._closed = True
        pumps = [s.pump_task for s in list(self._sessions.values()) if s.pump_task is not None]
        for task in pumps:
            task.cancel()
        if pumps:
            await asyncio.gather(*pumps, return_exceptions=True)
        self._sessions.clear()
        try:
            self.store.clear_presence()
        except Exception:
            pass
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

    def _next_connection_id(self) -> int:
        self._connection_seq += 1
        return self._connection_seq

    async def attach(
        self,
        *,
        member_id: str,
        display_name: str,
        send: SendFn,
        resume_token: str = "",
        kind: str = "human",
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
        offered_token = str(resume_token or "").strip()

        async with self._lock:
            previous = self._sessions.get(member_id)
            if previous is not None:
                # One body, one place — but only the same body may take the place over.
                if not offered_token or not secrets.compare_digest(offered_token, previous.resume_token):
                    raise MemberTaken(member_id)
            connection_id = self._next_connection_id()
            if previous is not None:
                self._enqueue(
                    previous,
                    encode_error(
                        "replaced",
                        "replaced by a newer connection for this member_id",
                    ),
                )
                previous.replaced.set()
                self._stop_pump(previous)

            member_kind = normalize_member_kind(kind)
            member = self.store.upsert_member(member_id, display_name, kind=member_kind)
            session = Session(
                member_id=member.id,
                display_name=member.display_name,
                member_kind=member_kind,
                connection_id=connection_id,
                send=send,
                present=self.store.is_present(member_id),
                resume_token=secrets.token_urlsafe(24),
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
        present = session.present
        del self._sessions[session.member_id]
        session.replaced.set()
        if session.pump_task is not asyncio.current_task():
            self._stop_pump(session)
        if not present or not self.store.is_present(session.member_id):
            return
        with self.store.transaction():
            self.store.set_presence(session.member_id, False)
            event = self.store.append_event(
                kind=EventKind.LEAVE,
                actor_id=session.member_id,
                text="",
                actor_name=session.display_name,
                actor_kind=session.member_kind,
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
        if session.lagged:
            return
        if self._enqueue(session, message):
            return
        session.lagged = True
        self._drain_queue(session)
        lagged = encode_server_message(
            "lagged",
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
                self._join(session)
            elif msg_type == "leave":
                self._leave(session)
            elif msg_type == "speak":
                self._speak(session, payload)
            elif msg_type == "sync":
                self._sync(session, payload)
            else:
                self._enqueue(session, encode_error("unknown_type", f"unknown type: {msg_type}"))

    async def welcome(self, session: Session) -> None:
        self._enqueue(
            session,
            encode_server_message(
                "welcome",
                protocol_version=PROTOCOL_VERSION,
                world_id=self.world_id,
                name=self.store.name,
                latest_seq=self.store.max_seq(),
                member_id=session.member_id,
                display_name=session.display_name,
                present=session.present,
                resume_token=session.resume_token,
                member_kind=session.member_kind,
                capabilities=list(PROTOCOL_CAPABILITIES),
            ),
        )

    def _join(self, session: Session) -> None:
        already = self.store.is_present(session.member_id)
        with self.store.transaction():
            self.store.set_presence(session.member_id, True)
            event = None
            if not already:
                event = self.store.append_event(
                    kind=EventKind.JOIN,
                    actor_id=session.member_id,
                    text="",
                    actor_name=session.display_name,
                    actor_kind=session.member_kind,
                )
        session.present = True
        session.lagged = False
        if event is not None:
            self._fanout(event, exclude={session.member_id})

        present = self.list_live_present()
        history = [e.to_dict(world_id=self.world_id) for e in self.store.recent_events(limit=SNAPSHOT_HISTORY_LIMIT)]
        self._enqueue(
            session,
            encode_server_message(
                "snapshot",
                name=self.store.name,
                present=present,
                events=history,
            ),
        )

    def _leave(self, session: Session) -> None:
        if not self.store.is_present(session.member_id):
            session.present = False
            self._enqueue(session, encode_error("not_present", "not present in this world"))
            return

        with self.store.transaction():
            self.store.set_presence(session.member_id, False)
            event = self.store.append_event(
                kind=EventKind.LEAVE,
                actor_id=session.member_id,
                text="",
                actor_name=session.display_name,
                actor_kind=session.member_kind,
            )
        session.present = False
        self._fanout(event)
        self._enqueue(session, encode_server_message("event", **event.to_dict(world_id=self.world_id)))

    def _speak(self, session: Session, payload: dict[str, Any]) -> None:
        text = str(payload.get("text") or "")
        attachments, attach_err = self._ingest_attachments(payload.get("attachments"))
        if attach_err:
            self._enqueue(session, attach_err)
            return
        if not text.strip() and not attachments:
            self._enqueue(session, encode_error("text_required", "text or attachments required"))
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
        mentions = self._normalize_mentions(text, mentions)
        if len(mentions) > MAX_MENTIONS:
            self._enqueue(
                session,
                encode_error("bad_payload", f"mentions exceeds {MAX_MENTIONS} entries"),
            )
            return

        if not self.store.is_present(session.member_id):
            self._enqueue(session, encode_error("not_present", "not present in this world"))
            return
        if not self._allow_speak(session.member_id):
            self._enqueue(session, encode_error("rate_limited", "speak rate exceeded"))
            return

        event = self.store.append_event(
            kind=EventKind.UTTERANCE,
            actor_id=session.member_id,
            text=text.strip(),
            mentions=mentions,
            attachments=attachments,
            actor_name=session.display_name,
            actor_kind=session.member_kind,
        )
        self._fanout(event)

    def _normalize_mentions(self, text: str, mentions: list[str]) -> list[str]:
        """Map @display_name to member_id when the match is unique among the live roster."""
        import re

        present = self.list_live_present()
        by_id = {p["member_id"]: p for p in present}
        by_name: dict[str, str] = {}
        for item in present:
            name = str(item.get("display_name") or "").strip()
            mid = str(item.get("member_id") or "").strip()
            if not name or not mid:
                continue
            key = name.casefold()
            if key in by_name and by_name[key] != mid:
                by_name[key] = ""
            elif key not in by_name:
                by_name[key] = mid

        out: list[str] = []
        seen: set[str] = set()

        def add(member_id: str) -> None:
            mid = str(member_id or "").strip()
            if not mid or mid in seen:
                return
            if mid not in by_id:
                return
            seen.add(mid)
            out.append(mid)

        for raw in mentions:
            add(raw)

        for match in re.finditer(r"@([^\s@.,!?，。！？]+)", text):
            token = match.group(1).strip()
            if not token:
                continue
            if token in by_id:
                add(token)
                continue
            mapped = by_name.get(token.casefold())
            if mapped:
                add(mapped)

        return out

    def _ingest_attachments(self, raw: Any) -> tuple[list[Attachment], Optional[str]]:
        if raw is None:
            return [], None
        if not isinstance(raw, list):
            return [], encode_error("bad_payload", "attachments must be a list")
        if len(raw) > MAX_ATTACHMENTS:
            return [], encode_error(
                "too_many_files",
                f"attachments exceeds {MAX_ATTACHMENTS} entries",
            )
        out: list[Attachment] = []
        total = 0
        for item in raw:
            if not isinstance(item, dict):
                return [], encode_error("bad_file", "attachment must be an object")
            data_b64 = item.get("data")
            file_id = str(item.get("id") or "").strip()
            if data_b64 is None and file_id:
                found = self.store.get_file(file_id)
                if found is None:
                    return [], encode_error("bad_file", f"unknown file: {file_id}")
                attachment, _path = found
                total += attachment.size
                if total > MAX_ATTACHMENTS_BYTES:
                    return [], encode_error(
                        "file_too_large",
                        f"attachments exceed {MAX_ATTACHMENTS_BYTES} bytes",
                    )
                out.append(attachment)
                continue
            if not isinstance(data_b64, str) or not data_b64.strip():
                return [], encode_error("bad_file", "attachment data is required")
            try:
                data = base64.b64decode(data_b64, validate=False)
            except Exception:
                return [], encode_error("bad_file", "attachment data is not base64")
            if not data:
                return [], encode_error("bad_file", "attachment data is empty")
            if len(data) > MAX_FILE_BYTES:
                return [], encode_error(
                    "file_too_large",
                    f"file exceeds {MAX_FILE_BYTES} bytes",
                )
            total += len(data)
            if total > MAX_ATTACHMENTS_BYTES:
                return [], encode_error(
                    "file_too_large",
                    f"attachments exceed {MAX_ATTACHMENTS_BYTES} bytes",
                )
            out.append(
                self.store.save_file(
                    name=str(item.get("name") or "file"),
                    mime=str(item.get("mime") or ""),
                    data=data,
                )
            )
        return out, None

    def _allow_speak(self, member_id: str) -> bool:
        now = self.clock.now()
        window = self._speak_times.setdefault(member_id, [])
        window[:] = [t for t in window if now - t < 1.0]
        if len(window) >= SPEAK_RATE_PER_SEC:
            return False
        window.append(now)
        return True

    def _sync(self, session: Session, payload: dict[str, Any]) -> None:
        raw_seq = payload.get("after_seq") if "after_seq" in payload else 0
        try:
            after_seq = int(raw_seq or 0)
        except (TypeError, ValueError):
            self._enqueue(session, encode_error("bad_payload", "after_seq must be an integer"))
            return
        if after_seq < 0:
            self._enqueue(session, encode_error("bad_payload", "after_seq must be >= 0"))
            return

        page_limit = SYNC_PAGE_SIZE
        fetched = self.store.events_after(after_seq, limit=page_limit + 1)
        has_more = len(fetched) > page_limit
        events = fetched[:page_limit]
        next_after_seq = events[-1].seq if events else after_seq
        session.lagged = False
        self._enqueue(
            session,
            encode_server_message(
                "snapshot",
                events=[e.to_dict(world_id=self.world_id) for e in events],
                sync=True,
                after_seq=after_seq,
                has_more=has_more,
                next_after_seq=next_after_seq,
            ),
        )

    def _fanout(self, event: WorldEvent, *, exclude: Optional[set[str]] = None) -> None:
        present_ids = {p.member_id for p in self.store.list_present()}
        message = encode_server_message("event", **self._perception_dict(event))
        skip = exclude or set()
        for member_id, session in list(self._sessions.items()):
            if member_id in skip:
                continue
            if member_id not in present_ids:
                continue
            if not session.present:
                continue
            self._enqueue_or_lag(session, message, event)

    def _perception_dict(self, event: WorldEvent) -> dict[str, Any]:
        body = event.to_dict(world_id=self.world_id)
        if not event.attachments:
            return body
        packed: list[dict[str, Any]] = []
        for item in event.attachments:
            entry = item.to_dict(world_id=self.world_id)
            found = self.store.get_file(item.id)
            if found is not None:
                _attachment, blob_path = found
                try:
                    data = blob_path.read_bytes()
                except OSError:
                    data = b""
                if data:
                    entry["data"] = base64.b64encode(data).decode("ascii")
            packed.append(entry)
        body["attachments"] = packed
        return body
