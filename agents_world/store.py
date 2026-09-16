"""SQLite-backed world store: members, presence, append-only events."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from . import WORLD_ACTOR_ID
from .clock import Clock, default_clock
from .files import new_file_id, normalize_mime, sanitize_file_name
from .models import Attachment, EventKind, Member, PresenceRecord, WorldEvent
from .config import WorldConfig
from .paths import validate_id, world_data_dir


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
            CREATE TABLE IF NOT EXISTS members (
                id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS presence (
                member_id TEXT PRIMARY KEY,
                present INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (member_id) REFERENCES members(id)
            );
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                kind TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                text TEXT NOT NULL,
                mentions_json TEXT NOT NULL DEFAULT '[]',
                attachments_json TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS files (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                mime TEXT NOT NULL,
                size INTEGER NOT NULL,
                ts REAL NOT NULL
            );
            """
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('world_id', ?)",
            (self.world_id,),
        )
        self._conn.commit()

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

    def bootstrap(self, config: WorldConfig) -> None:
        self.set_meta("name", config.name)

    @property
    def name(self) -> str:
        return self.get_meta("name") or self.world_id

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

    def set_presence(self, member_id: str, present: bool) -> None:
        self._conn.execute(
            """
            INSERT INTO presence(member_id, present) VALUES (?, ?)
            ON CONFLICT(member_id) DO UPDATE SET present=excluded.present
            """,
            (member_id, 1 if present else 0),
        )
        self._commit()

    def list_present(self) -> list[PresenceRecord]:
        rows = self._conn.execute(
            """
            SELECT p.member_id, m.display_name, p.present
            FROM presence p
            JOIN members m ON m.id = p.member_id
            WHERE p.present = 1
            ORDER BY m.display_name, p.member_id
            """
        ).fetchall()
        return [
            PresenceRecord(
                member_id=r["member_id"],
                display_name=r["display_name"],
                present=bool(r["present"]),
            )
            for r in rows
        ]

    def is_present(self, member_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM presence WHERE member_id = ? AND present = 1 LIMIT 1",
            (member_id,),
        ).fetchone()
        return row is not None

    def clear_presence(self) -> None:
        """Nobody is here until they join this process. Crash/restart must not ghost people."""
        self._conn.execute("UPDATE presence SET present = 0")
        self._commit()

    def files_dir(self) -> Path:
        path = self.db_path.parent / "files"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_file(self, *, name: str, mime: str, data: bytes) -> Attachment:
        file_id = new_file_id()
        filename = sanitize_file_name(name)
        content_type = normalize_mime(mime)
        blob_path = (self.files_dir() / file_id).resolve()
        blob_path.relative_to(self.files_dir().resolve())
        blob_path.write_bytes(data)
        ts = float(self.clock.now())
        self._conn.execute(
            "INSERT INTO files(id, name, mime, size, ts) VALUES (?, ?, ?, ?, ?)",
            (file_id, filename, content_type, len(data), ts),
        )
        self._commit()
        return Attachment(id=file_id, name=filename, mime=content_type, size=len(data))

    def get_file(self, file_id: str) -> Optional[tuple[Attachment, Path]]:
        try:
            file_id = validate_id(file_id, label="file_id")
        except ValueError:
            return None
        row = self._conn.execute(
            "SELECT id, name, mime, size FROM files WHERE id = ?",
            (file_id,),
        ).fetchone()
        if row is None:
            return None
        path = (self.files_dir() / file_id).resolve()
        try:
            path.relative_to(self.files_dir().resolve())
        except ValueError:
            return None
        if not path.is_file():
            return None
        return (
            Attachment(
                id=str(row["id"]),
                name=str(row["name"]),
                mime=str(row["mime"]),
                size=int(row["size"]),
            ),
            path,
        )

    def append_event(
        self,
        *,
        kind: EventKind | str,
        actor_id: str,
        text: str,
        mentions: Optional[list[str] | tuple[str, ...]] = None,
        attachments: Optional[list[Attachment] | tuple[Attachment, ...]] = None,
        ts: Optional[float] = None,
    ) -> WorldEvent:
        event_ts = float(ts if ts is not None else self.clock.now())
        mention_list = [str(m).strip() for m in (mentions or []) if str(m).strip()]
        mentions_json = json.dumps(mention_list, ensure_ascii=False)
        attachment_items = tuple(attachments or ())
        attachments_json = json.dumps(
            [item.to_dict(world_id=self.world_id) for item in attachment_items],
            ensure_ascii=False,
        )
        kind_value = kind.value if isinstance(kind, EventKind) else str(kind)
        cur = self._conn.execute(
            """
            INSERT INTO events(ts, kind, actor_id, text, mentions_json, attachments_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_ts, kind_value, actor_id, text, mentions_json, attachments_json),
        )
        self._commit()
        seq = int(cur.lastrowid)
        return WorldEvent(
            seq=seq,
            ts=event_ts,
            kind=kind,
            actor_id=actor_id,
            text=text,
            mentions=tuple(mention_list),
            attachments=attachment_items,
        )

    def events_after(
        self,
        after_seq: int = 0,
        *,
        limit: int = 200,
    ) -> list[WorldEvent]:
        rows = self._conn.execute(
            """
            SELECT seq, ts, kind, actor_id, text, mentions_json, attachments_json
            FROM events
            WHERE seq > ?
            ORDER BY seq ASC
            LIMIT ?
            """,
            (after_seq, limit),
        ).fetchall()
        return [_event_from_row(r) for r in rows]

    def recent_events(self, *, limit: int = 50) -> list[WorldEvent]:
        rows = self._conn.execute(
            """
            SELECT seq, ts, kind, actor_id, text, mentions_json, attachments_json
            FROM events
            ORDER BY seq DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        events = [_event_from_row(r) for r in rows]
        events.reverse()
        return events

    def max_seq(self) -> int:
        row = self._conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM events").fetchone()
        return int(row["m"] if row else 0)


def _event_from_row(row: sqlite3.Row) -> WorldEvent:
    return WorldEvent.from_row(**dict(row))


def open_store_for_world(
    config: WorldConfig,
    *,
    root: Optional[Path] = None,
    clock: Optional[Clock] = None,
) -> WorldStore:
    data_dir = world_data_dir(config.world_id, root=root)
    store = WorldStore(data_dir / "world.sqlite3", world_id=config.world_id, clock=clock)
    store.bootstrap(config)
    store.upsert_member(WORLD_ACTOR_ID, "world")
    store.ensure_world_epoch()
    return store
