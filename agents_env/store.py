"""SQLite-backed world store: rooms, members, presence, append-only events."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Optional

from . import WORLD_ACTOR_ID
from .clock import Clock, default_clock
from .models import EventKind, Member, PresenceRecord, Room, WorldEvent
from .paths import world_data_dir
from .scene import SceneConfig


class WorldStore:
    """Durable venue state. Never imports or writes agent memory."""

    def __init__(self, db_path: Path, *, world_id: str, clock: Optional[Clock] = None):
        self.db_path = Path(db_path)
        self.world_id = world_id
        self.clock = default_clock(clock)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._tx_depth = 0
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator["WorldStore"]:
        self._tx_depth += 1
        try:
            yield self
            self._tx_depth -= 1
            if self._tx_depth == 0:
                self._conn.commit()
        except Exception:
            self._tx_depth = 0
            self._conn.rollback()
            raise

    def _commit(self) -> None:
        if self._tx_depth == 0:
            self._conn.commit()

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
                room_seq INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (room_id) REFERENCES rooms(id)
            );
            CREATE INDEX IF NOT EXISTS idx_events_room_seq ON events(room_id, seq);
            CREATE INDEX IF NOT EXISTS idx_events_room_room_seq ON events(room_id, room_seq);
            """
        )
        self._ensure_room_seq_column()
        self._conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('world_id', ?)",
            (self.world_id,),
        )
        self._conn.commit()

    def _ensure_room_seq_column(self) -> None:
        cols = {str(row["name"]) for row in self._conn.execute("PRAGMA table_info(events)")}
        if "room_seq" not in cols:
            self._conn.execute(
                "ALTER TABLE events ADD COLUMN room_seq INTEGER NOT NULL DEFAULT 0"
            )
        missing = self._conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE room_seq = 0"
        ).fetchone()
        if missing and int(missing["n"]) > 0:
            self._conn.execute(
                """
                UPDATE events
                SET room_seq = (
                    SELECT COUNT(*)
                    FROM events AS e2
                    WHERE e2.room_id = events.room_id AND e2.seq <= events.seq
                )
                WHERE room_seq = 0
                """
            )

    def get_meta(self, key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            (key, value),
        )
        self._commit()

    def ensure_world_epoch(self) -> float:
        raw = self.get_meta("world_epoch")
        if raw:
            return float(raw)
        epoch = float(self.clock.now())
        self.set_meta("world_epoch", str(epoch))
        return epoch

    def scene_already_fired(self, key: str) -> bool:
        return self.get_meta(scene_meta_key(key)) is not None

    def mark_scene_fired(self, key: str, *, seq: int) -> None:
        self.set_meta(scene_meta_key(key), str(seq))

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
        self._commit()

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
        self._commit()
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
        self._commit()

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
        kind: EventKind | str,
        actor_id: str,
        text: str,
        mentions: Optional[list[str] | tuple[str, ...]] = None,
        ts: Optional[float] = None,
    ) -> WorldEvent:
        event_ts = float(ts if ts is not None else self.clock.now())
        mention_list = [str(m).strip() for m in (mentions or []) if str(m).strip()]
        mentions_json = json.dumps(mention_list, ensure_ascii=False)
        kind_value = kind.value if isinstance(kind, EventKind) else str(kind)
        cur = self._conn.execute(
            """
            INSERT INTO events(ts, room_id, kind, actor_id, text, mentions_json, room_seq)
            VALUES (
                ?, ?, ?, ?, ?, ?,
                (SELECT COALESCE(MAX(room_seq), 0) + 1 FROM events WHERE room_id = ?)
            )
            """,
            (event_ts, room_id, kind_value, actor_id, text, mentions_json, room_id),
        )
        self._commit()
        seq = int(cur.lastrowid)
        row = self._conn.execute(
            "SELECT room_seq FROM events WHERE seq = ?", (seq,)
        ).fetchone()
        room_seq = int(row["room_seq"]) if row else 0
        return WorldEvent(
            seq=seq,
            ts=event_ts,
            room_id=room_id,
            kind=kind,
            actor_id=actor_id,
            text=text,
            mentions=tuple(mention_list),
            room_seq=room_seq,
        )

    def events_after(
        self,
        room_id: str,
        after_seq: int = 0,
        *,
        limit: int = 200,
    ) -> list[WorldEvent]:
        rows = self._conn.execute(
            """
            SELECT seq, ts, room_id, kind, actor_id, text, mentions_json, room_seq
            FROM events
            WHERE room_id = ? AND seq > ?
            ORDER BY seq ASC
            LIMIT ?
            """,
            (room_id, after_seq, limit),
        ).fetchall()
        return [_event_from_row(r) for r in rows]

    def recent_events(self, room_id: str, *, limit: int = 50) -> list[WorldEvent]:
        rows = self._conn.execute(
            """
            SELECT seq, ts, room_id, kind, actor_id, text, mentions_json, room_seq
            FROM events
            WHERE room_id = ?
            ORDER BY seq DESC
            LIMIT ?
            """,
            (room_id, limit),
        ).fetchall()
        events = [_event_from_row(r) for r in rows]
        events.reverse()
        return events

    def room_heads(self) -> dict[str, dict[str, int]]:
        heads = {
            room.id: {"latest_seq": 0, "latest_room_seq": 0} for room in self.list_rooms()
        }
        rows = self._conn.execute(
            """
            SELECT room_id, MAX(seq) AS seq, MAX(room_seq) AS room_seq
            FROM events
            GROUP BY room_id
            """
        ).fetchall()
        for row in rows:
            heads[str(row["room_id"])] = {
                "latest_seq": int(row["seq"] or 0),
                "latest_room_seq": int(row["room_seq"] or 0),
            }
        return heads

    def max_seq(self) -> int:
        row = self._conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM events").fetchone()
        return int(row["m"] if row else 0)


def _event_from_row(row: sqlite3.Row) -> WorldEvent:
    return WorldEvent.from_row(**dict(row))


def scene_meta_key(key: str) -> str:
    return f"scene_fired:{key}"


def open_store_for_scene(
    scene: SceneConfig,
    *,
    root: Optional[Path] = None,
    clock: Optional[Clock] = None,
) -> WorldStore:
    data_dir = world_data_dir(scene.world_id, root=root)
    store = WorldStore(data_dir / "world.sqlite3", world_id=scene.world_id, clock=clock)
    store.bootstrap_rooms(scene.rooms)
    store.upsert_member(WORLD_ACTOR_ID, "world")
    store.ensure_world_epoch()
    return store
