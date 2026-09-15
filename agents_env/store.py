"""SQLite-backed world store: rooms, members, presence, append-only events."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Iterable, Optional

from . import WORLD_ACTOR_ID
from .models import EventKind, Member, PresenceRecord, Room, WorldEvent
from .scene import SceneConfig


class WorldStore:
    """Durable venue state. Never imports or writes agent memory."""

    def __init__(self, db_path: Path, *, world_id: str):
        self.db_path = Path(db_path)
        self.world_id = world_id
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rooms (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                setting TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS members (
                id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS presence (
                member_id TEXT NOT NULL,
                room_id TEXT NOT NULL,
                present INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (member_id, room_id),
                FOREIGN KEY (member_id) REFERENCES members(id),
                FOREIGN KEY (room_id) REFERENCES rooms(id)
            );
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                room_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                text TEXT NOT NULL,
                mentions_json TEXT NOT NULL DEFAULT '[]',
                FOREIGN KEY (room_id) REFERENCES rooms(id)
            );
            """
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('world_id', ?)",
            (self.world_id,),
        )
        self._conn.commit()

    def bootstrap_rooms(self, rooms: Iterable[Room]) -> None:
        for room in rooms:
            self._conn.execute(
                """
                INSERT INTO rooms(id, name, setting) VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    setting=excluded.setting
                """,
                (room.id, room.name, room.setting),
            )
        self._conn.commit()

    def list_rooms(self) -> list[Room]:
        rows = self._conn.execute(
            "SELECT id, name, setting FROM rooms ORDER BY id"
        ).fetchall()
        return [Room(id=r["id"], name=r["name"], setting=r["setting"]) for r in rows]

    def get_room(self, room_id: str) -> Optional[Room]:
        row = self._conn.execute(
            "SELECT id, name, setting FROM rooms WHERE id = ?",
            (room_id,),
        ).fetchone()
        if row is None:
            return None
        return Room(id=row["id"], name=row["name"], setting=row["setting"])

    def upsert_member(self, member_id: str, display_name: str) -> Member:
        name = display_name.strip() or member_id
        self._conn.execute(
            """
            INSERT INTO members(id, display_name) VALUES (?, ?)
            ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name
            """,
            (member_id, name),
        )
        self._conn.commit()
        return Member(id=member_id, display_name=name)

    def get_member(self, member_id: str) -> Optional[Member]:
        row = self._conn.execute(
            "SELECT id, display_name FROM members WHERE id = ?",
            (member_id,),
        ).fetchone()
        if row is None:
            return None
        return Member(id=row["id"], display_name=row["display_name"])

    def set_presence(self, member_id: str, room_id: str, present: bool) -> None:
        self._conn.execute(
            """
            INSERT INTO presence(member_id, room_id, present) VALUES (?, ?, ?)
            ON CONFLICT(member_id, room_id) DO UPDATE SET present=excluded.present
            """,
            (member_id, room_id, 1 if present else 0),
        )
        self._conn.commit()

    def clear_member_presence(self, member_id: str) -> list[str]:
        """Mark member absent everywhere. Returns rooms they left."""
        rows = self._conn.execute(
            "SELECT room_id FROM presence WHERE member_id = ? AND present = 1",
            (member_id,),
        ).fetchall()
        room_ids = [str(r["room_id"]) for r in rows]
        if room_ids:
            self._conn.execute(
                "UPDATE presence SET present = 0 WHERE member_id = ?",
                (member_id,),
            )
            self._conn.commit()
        return room_ids

    def list_present(self, room_id: str) -> list[PresenceRecord]:
        rows = self._conn.execute(
            """
            SELECT p.member_id, p.room_id, m.display_name, p.present
            FROM presence p
            JOIN members m ON m.id = p.member_id
            WHERE p.room_id = ? AND p.present = 1
            ORDER BY m.display_name, p.member_id
            """,
            (room_id,),
        ).fetchall()
        return [
            PresenceRecord(
                member_id=r["member_id"],
                room_id=r["room_id"],
                display_name=r["display_name"],
                present=bool(r["present"]),
            )
            for r in rows
        ]

    def is_present(self, member_id: str, room_id: str) -> bool:
        row = self._conn.execute(
            "SELECT present FROM presence WHERE member_id = ? AND room_id = ?",
            (member_id, room_id),
        ).fetchone()
        return bool(row and row["present"])

    def append_event(
        self,
        *,
        room_id: str,
        kind: EventKind,
        actor_id: str,
        text: str,
        mentions: Optional[Iterable[str]] = None,
        ts: Optional[float] = None,
    ) -> WorldEvent:
        event_ts = float(ts if ts is not None else time.time())
        mention_list = [str(m).strip() for m in (mentions or []) if str(m).strip()]
        mentions_json = json.dumps(mention_list, ensure_ascii=False)
        cur = self._conn.execute(
            """
            INSERT INTO events(ts, room_id, kind, actor_id, text, mentions_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_ts, room_id, kind.value, actor_id, text, mentions_json),
        )
        self._conn.commit()
        seq = int(cur.lastrowid)
        return WorldEvent(
            seq=seq,
            ts=event_ts,
            room_id=room_id,
            kind=kind,
            actor_id=actor_id,
            text=text,
            mentions=tuple(mention_list),
        )

    def events_after(self, room_id: str, after_seq: int = 0, *, limit: int = 200) -> list[WorldEvent]:
        rows = self._conn.execute(
            """
            SELECT seq, ts, room_id, kind, actor_id, text, mentions_json
            FROM events
            WHERE room_id = ? AND seq > ?
            ORDER BY seq ASC
            LIMIT ?
            """,
            (room_id, after_seq, limit),
        ).fetchall()
        return [WorldEvent.from_row(**dict(r)) for r in rows]

    def recent_events(self, room_id: str, *, limit: int = 50) -> list[WorldEvent]:
        rows = self._conn.execute(
            """
            SELECT seq, ts, room_id, kind, actor_id, text, mentions_json
            FROM events
            WHERE room_id = ?
            ORDER BY seq DESC
            LIMIT ?
            """,
            (room_id, limit),
        ).fetchall()
        events = [WorldEvent.from_row(**dict(r)) for r in rows]
        events.reverse()
        return events

    def max_seq(self) -> int:
        row = self._conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM events").fetchone()
        return int(row["m"] if row else 0)


def world_data_dir(world_id: str, *, root: Optional[Path] = None) -> Path:
    from .paths import resolve_data_root

    base = resolve_data_root(root)
    return base / "worlds" / world_id


def open_store_for_scene(scene: SceneConfig, *, root: Optional[Path] = None) -> WorldStore:
    data_dir = world_data_dir(scene.world_id, root=root)
    store = WorldStore(data_dir / "world.sqlite3", world_id=scene.world_id)
    store.bootstrap_rooms(scene.rooms)
    # Ensure world actor exists as a member name for display clarity (optional).
    store.upsert_member(WORLD_ACTOR_ID, "world")
    return store
