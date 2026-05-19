"""Unified SQLite-backed experience and long-term memory store."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from ..sqlite_query import execute_readonly_query
from ...schemas import Message, MessageType, RoleType

logger = logging.getLogger(__name__)


class ExperienceMemoryStoreConfig:
    """Configuration constants for ``ExperienceMemoryStore``."""

    DEFAULT_PATH = "~/.xagent/memory/xagent_memory.sqlite3"
    MEMORY_DB_FILENAME = "xagent_memory.sqlite3"
    CONNECT_TIMEOUT = 5.0
    SCHEMA_VERSION = 1
    DEFAULT_RECALL_LIMIT = 8
    HARD_LIMIT = 500


class MemoryKind:
    EPISODIC = "episodic"
    SEMANTIC_FACT = "semantic_fact"
    PREFERENCE = "preference"
    COMMITMENT = "commitment"
    PROJECT_STATE = "project_state"
    PERSON_FACT = "person_fact"
    PROCEDURE = "procedure"
    SUMMARY = "summary"

    VALUES = {
        EPISODIC,
        SEMANTIC_FACT,
        PREFERENCE,
        COMMITMENT,
        PROJECT_STATE,
        PERSON_FACT,
        PROCEDURE,
        SUMMARY,
    }


class SubjectType:
    SELF = "self"
    PERSON = "person"
    PROJECT = "project"
    TOPIC = "topic"
    ROOM = "room"
    SYSTEM = "system"

    VALUES = {SELF, PERSON, PROJECT, TOPIC, ROOM, SYSTEM}


class MemoryStatus:
    CANDIDATE = "candidate"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"
    ARCHIVED = "archived"

    VALUES = {CANDIDATE, ACTIVE, SUPERSEDED, RETRACTED, ARCHIVED}


class Sensitivity:
    NORMAL = "normal"
    PRIVATE = "private"
    SENSITIVE = "sensitive"
    SECRET = "secret"

    VALUES = {NORMAL, PRIVATE, SENSITIVE, SECRET}


@dataclass(frozen=True)
class TimeRange:
    start: Optional[float] = None
    end: Optional[float] = None


MessageBatch = Message | Sequence[Message]


class ExperienceMemoryStore:
    """Canonical store for raw events, structured memories, evidence, and revisions."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = Path(path or ExperienceMemoryStoreConfig.DEFAULT_PATH).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger(self.__class__.__name__)
        self._write_lock = asyncio.Lock()
        self._initialize_database()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path),
            timeout=ExperienceMemoryStoreConfig.CONNECT_TIMEOUT,
        )
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            self._create_schema(connection)
            connection.commit()

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at REAL NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO schema_migrations (version, applied_at)
            VALUES (?, ?)
            """,
            (ExperienceMemoryStoreConfig.SCHEMA_VERSION, time.time()),
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc REAL NOT NULL,
                timezone TEXT NOT NULL,
                channel TEXT,
                conversation_id TEXT,
                room_id TEXT,
                speaker_id TEXT,
                role TEXT NOT NULL,
                event_type TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                privacy_scope TEXT NOT NULL DEFAULT 'normal',
                sensitivity TEXT NOT NULL DEFAULT 'normal',
                content_hash TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                subject_type TEXT NOT NULL,
                subject_key TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                salience REAL NOT NULL DEFAULT 0.7,
                confidence REAL NOT NULL DEFAULT 0.8,
                sensitivity TEXT NOT NULL DEFAULT 'normal',
                valid_from REAL,
                valid_until REAL,
                observed_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_accessed_at REAL,
                access_count INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id INTEGER NOT NULL,
                event_id INTEGER,
                quote TEXT NOT NULL,
                relation TEXT NOT NULL DEFAULT 'supports',
                confidence REAL NOT NULL DEFAULT 0.8,
                extractor_model TEXT,
                created_at REAL NOT NULL,
                FOREIGN KEY(memory_id) REFERENCES memory_items(id) ON DELETE CASCADE,
                FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE SET NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id INTEGER,
                revision_type TEXT NOT NULL,
                old_content TEXT,
                new_content TEXT,
                reason TEXT NOT NULL,
                actor TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY(memory_id) REFERENCES memory_items(id) ON DELETE SET NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS people (
                person_key TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                aliases_json TEXT NOT NULL DEFAULT '[]',
                relationship TEXT,
                notes TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                summary_type TEXT NOT NULL,
                scope_type TEXT NOT NULL,
                scope_key TEXT NOT NULL,
                period_start REAL,
                period_end REAL,
                content TEXT NOT NULL,
                source_memory_ids_json TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS retention_policies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_type TEXT NOT NULL,
                scope_key TEXT NOT NULL,
                policy TEXT NOT NULL,
                ttl_days INTEGER,
                created_at REAL NOT NULL
            )
            """
        )
        self._create_indexes(connection)
        self._create_fts(connection)

    def _create_indexes(self, connection: sqlite3.Connection) -> None:
        statements = [
            "CREATE INDEX IF NOT EXISTS idx_events_time ON events(timestamp_utc)",
            "CREATE INDEX IF NOT EXISTS idx_events_speaker ON events(speaker_id)",
            "CREATE INDEX IF NOT EXISTS idx_events_conversation ON events(conversation_id)",
            "CREATE INDEX IF NOT EXISTS idx_events_room ON events(room_id)",
            "CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)",
            "CREATE INDEX IF NOT EXISTS idx_memory_status ON memory_items(status)",
            "CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory_items(kind)",
            "CREATE INDEX IF NOT EXISTS idx_memory_subject ON memory_items(subject_type, subject_key)",
            "CREATE INDEX IF NOT EXISTS idx_memory_updated ON memory_items(updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_memory_observed ON memory_items(observed_at)",
            "CREATE INDEX IF NOT EXISTS idx_evidence_memory ON memory_evidence(memory_id)",
            "CREATE INDEX IF NOT EXISTS idx_evidence_event ON memory_evidence(event_id)",
            "CREATE INDEX IF NOT EXISTS idx_summary_scope ON memory_summaries(scope_type, scope_key)",
        ]
        for statement in statements:
            connection.execute(statement)

    def _create_fts(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS events_fts
            USING fts5(content, content='events', content_rowid='id')
            """
        )
        connection.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_items_fts
            USING fts5(title, content, content='memory_items', content_rowid='id')
            """
        )
        fts_triggers = [
            """
            CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
                INSERT INTO events_fts(rowid, content) VALUES (new.id, new.content);
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS events_ad AFTER DELETE ON events BEGIN
                INSERT INTO events_fts(events_fts, rowid, content)
                VALUES('delete', old.id, old.content);
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS events_au AFTER UPDATE ON events BEGIN
                INSERT INTO events_fts(events_fts, rowid, content)
                VALUES('delete', old.id, old.content);
                INSERT INTO events_fts(rowid, content) VALUES (new.id, new.content);
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS memory_items_ai AFTER INSERT ON memory_items BEGIN
                INSERT INTO memory_items_fts(rowid, title, content)
                VALUES (new.id, new.title, new.content);
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS memory_items_ad AFTER DELETE ON memory_items BEGIN
                INSERT INTO memory_items_fts(memory_items_fts, rowid, title, content)
                VALUES('delete', old.id, old.title, old.content);
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS memory_items_au AFTER UPDATE ON memory_items BEGIN
                INSERT INTO memory_items_fts(memory_items_fts, rowid, title, content)
                VALUES('delete', old.id, old.title, old.content);
                INSERT INTO memory_items_fts(rowid, title, content)
                VALUES (new.id, new.title, new.content);
            END
            """,
        ]
        for statement in fts_triggers:
            connection.execute(statement)

    async def add_event(
        self,
        *,
        content: str,
        role: str,
        event_type: str,
        timestamp_utc: Optional[float] = None,
        timezone_name: str = "UTC",
        channel: Optional[str] = None,
        conversation_id: Optional[str] = None,
        room_id: Optional[str] = None,
        speaker_id: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        privacy_scope: str = "normal",
        sensitivity: str = Sensitivity.NORMAL,
    ) -> int:
        """Append one raw experience event and return its id."""
        normalized_content = str(content or "").strip()
        if not normalized_content:
            return 0
        payload = {
            "timestamp_utc": float(timestamp_utc if timestamp_utc is not None else time.time()),
            "timezone": str(timezone_name or "UTC"),
            "channel": channel,
            "conversation_id": conversation_id,
            "room_id": room_id,
            "speaker_id": speaker_id,
            "role": str(role or RoleType.USER.value),
            "event_type": str(event_type or MessageType.Message.value),
            "content": normalized_content,
            "metadata_json": self._json_dumps(dict(metadata or {})),
            "privacy_scope": str(privacy_scope or "normal"),
            "sensitivity": self._normalize_sensitivity(sensitivity),
            "content_hash": self._content_hash(normalized_content),
        }
        async with self._write_lock:
            return await asyncio.to_thread(self._insert_event_sync, payload)

    def _insert_event_sync(self, payload: Mapping[str, Any]) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO events (
                    timestamp_utc, timezone, channel, conversation_id, room_id,
                    speaker_id, role, event_type, content, metadata_json,
                    privacy_scope, sensitivity, content_hash
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["timestamp_utc"],
                    payload["timezone"],
                    payload.get("channel"),
                    payload.get("conversation_id"),
                    payload.get("room_id"),
                    payload.get("speaker_id"),
                    payload["role"],
                    payload["event_type"],
                    payload["content"],
                    payload["metadata_json"],
                    payload["privacy_scope"],
                    payload["sensitivity"],
                    payload["content_hash"],
                ),
            )
            connection.commit()
            return int(cursor.lastrowid or 0)

    async def add_messages(self, messages: MessageBatch, **kwargs) -> None:
        """Persist ``Message`` objects as raw events."""
        normalized = self.normalize_messages(messages)
        if not normalized:
            return
        async with self._write_lock:
            event_ids = await asyncio.to_thread(self._add_messages_sync, normalized)
        for message, event_id in zip(normalized, event_ids):
            if event_id:
                message.metadata["event_id"] = event_id

    def _add_messages_sync(self, messages: Sequence[Message]) -> list[int]:
        event_ids: list[int] = []
        with self._connect() as connection:
            for message in messages:
                metadata = dict(message.metadata or {})
                timezone_name = str(metadata.get("timezone") or "UTC")
                payload = message.model_dump(mode="json")
                metadata["message_json"] = payload
                content = str(message.content or "")
                cursor = connection.execute(
                    """
                    INSERT INTO events (
                        timestamp_utc, timezone, channel, conversation_id, room_id,
                        speaker_id, role, event_type, content, metadata_json,
                        privacy_scope, sensitivity, content_hash
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        float(message.timestamp),
                        timezone_name,
                        metadata.get("channel"),
                        metadata.get("conversation_id"),
                        metadata.get("room_id"),
                        message.sender_id,
                        message.role.value if hasattr(message.role, "value") else str(message.role),
                        message.type.value if hasattr(message.type, "value") else str(message.type),
                        content,
                        self._json_dumps(metadata),
                        str(metadata.get("privacy_scope") or "normal"),
                        self._normalize_sensitivity(metadata.get("sensitivity") or Sensitivity.NORMAL),
                        self._content_hash(content),
                    ),
                )
                event_id = int(cursor.lastrowid or 0)
                metadata["event_id"] = event_id
                metadata["message_json"]["metadata"] = {
                    **dict(payload.get("metadata") or {}),
                    "event_id": event_id,
                }
                connection.execute(
                    "UPDATE events SET metadata_json = ? WHERE id = ?",
                    (self._json_dumps(metadata), event_id),
                )
                event_ids.append(event_id)
            connection.commit()
        return event_ids

    async def get_messages(self, count: int = 20, offset: int = 0) -> list[Message]:
        count, offset = self.validate_pagination(count, offset)
        return await asyncio.to_thread(self._get_messages_sync, count, offset)

    def _get_messages_sync(self, count: int, offset: int = 0) -> list[Message]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM events
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (count, offset),
            ).fetchall()
        messages: list[Message] = []
        for row in reversed(rows):
            message = self._message_from_event_row(row)
            if message is not None:
                messages.append(message)
        return messages

    async def clear_messages(self) -> None:
        async with self._write_lock:
            await asyncio.to_thread(self._clear_events_sync)

    def _clear_events_sync(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM events")
            connection.commit()

    async def pop_message(self) -> Optional[Message]:
        async with self._write_lock:
            return await asyncio.to_thread(self._pop_message_sync)

    def _pop_message_sync(self) -> Optional[Message]:
        with self._connect() as connection:
            while True:
                row = connection.execute(
                    """
                    SELECT *
                    FROM events
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    return None
                connection.execute("DELETE FROM events WHERE id = ?", (row["id"],))
                connection.commit()
                message = self._message_from_event_row(row)
                if message is None or message.type in {MessageType.FUNCTION_CALL, MessageType.FUNCTION_CALL_OUTPUT}:
                    continue
                return message

    async def get_message_count(self) -> int:
        return await asyncio.to_thread(self._count_table_sync, "events")

    async def remember(
        self,
        content: str,
        *,
        kind: str = MemoryKind.SEMANTIC_FACT,
        subject_type: str = SubjectType.SELF,
        subject_key: str = "self",
        title: Optional[str] = None,
        status: str = MemoryStatus.ACTIVE,
        salience: float = 0.7,
        confidence: float = 0.8,
        sensitivity: str = Sensitivity.NORMAL,
        valid_from: Optional[float | str | date] = None,
        valid_until: Optional[float | str | date] = None,
        observed_at: Optional[float | str | date] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        evidence_event_ids: Optional[Iterable[int]] = None,
        evidence_note: Optional[str] = None,
        relation: str = "supports",
        extractor_model: Optional[str] = None,
    ) -> int:
        """Create or merge one long-term memory item."""
        normalized_content = str(content or "").strip()
        if not normalized_content:
            return 0
        normalized_kind = self._normalize_kind(kind)
        normalized_subject_type = self._normalize_subject_type(subject_type)
        normalized_status = self._normalize_status(status)
        normalized_sensitivity = self._normalize_sensitivity(sensitivity)
        now = time.time()
        item = {
            "kind": normalized_kind,
            "subject_type": normalized_subject_type,
            "subject_key": str(subject_key or normalized_subject_type),
            "title": str(title or self._default_title(normalized_kind, normalized_content)).strip(),
            "content": normalized_content,
            "status": normalized_status,
            "salience": self._clamp01(salience),
            "confidence": self._clamp01(confidence),
            "sensitivity": normalized_sensitivity,
            "valid_from": self._coerce_time(valid_from),
            "valid_until": self._coerce_time(valid_until),
            "observed_at": self._coerce_time(observed_at) or now,
            "created_at": now,
            "updated_at": now,
            "metadata_json": self._json_dumps(dict(metadata or {})),
        }
        evidence_ids = [int(event_id) for event_id in evidence_event_ids or [] if event_id]
        async with self._write_lock:
            return await asyncio.to_thread(
                self._remember_sync,
                item,
                evidence_ids,
                evidence_note,
                relation,
                extractor_model,
            )

    def _remember_sync(
        self,
        item: Mapping[str, Any],
        evidence_event_ids: Sequence[int],
        evidence_note: Optional[str],
        relation: str,
        extractor_model: Optional[str],
    ) -> int:
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT id, confidence, salience
                FROM memory_items
                WHERE kind = ?
                AND subject_type = ?
                AND subject_key = ?
                AND content = ?
                AND status IN ('active', 'candidate')
                LIMIT 1
                """,
                (
                    item["kind"],
                    item["subject_type"],
                    item["subject_key"],
                    item["content"],
                ),
            ).fetchone()
            if existing is not None:
                memory_id = int(existing["id"])
                connection.execute(
                    """
                    UPDATE memory_items
                    SET confidence = MAX(confidence, ?),
                        salience = MAX(salience, ?),
                        updated_at = ?,
                        status = CASE WHEN status = 'candidate' AND ? = 'active' THEN 'active' ELSE status END
                    WHERE id = ?
                    """,
                    (
                        item["confidence"],
                        item["salience"],
                        item["updated_at"],
                        item["status"],
                        memory_id,
                    ),
                )
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO memory_items (
                        kind, subject_type, subject_key, title, content, status,
                        salience, confidence, sensitivity, valid_from, valid_until,
                        observed_at, created_at, updated_at, metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["kind"],
                        item["subject_type"],
                        item["subject_key"],
                        item["title"],
                        item["content"],
                        item["status"],
                        item["salience"],
                        item["confidence"],
                        item["sensitivity"],
                        item["valid_from"],
                        item["valid_until"],
                        item["observed_at"],
                        item["created_at"],
                        item["updated_at"],
                        item["metadata_json"],
                    ),
                )
                memory_id = int(cursor.lastrowid or 0)
            self._insert_evidence_sync(
                connection,
                memory_id=memory_id,
                event_ids=evidence_event_ids,
                quote=evidence_note or item["content"],
                relation=relation,
                confidence=float(item["confidence"]),
                extractor_model=extractor_model,
            )
            if item["subject_type"] == SubjectType.PERSON:
                self._upsert_person_sync(
                    connection,
                    person_key=item["subject_key"],
                    display_name=str(item["subject_key"]),
                )
            connection.commit()
            return memory_id

    async def recall_memory(
        self,
        query: str = "",
        *,
        subject_type: Optional[str] = None,
        subject_key: Optional[str] = None,
        time_range: Any = None,
        kinds: Optional[Sequence[str] | str] = None,
        include_evidence: bool = False,
        max_items: int = ExperienceMemoryStoreConfig.DEFAULT_RECALL_LIMIT,
        allowed_sensitivities: Sequence[str] = (Sensitivity.NORMAL,),
    ) -> dict[str, Any]:
        """Recall active memory items using filters, FTS, and deterministic ranking."""
        normalized_limit = self._normalize_limit(max_items, default=ExperienceMemoryStoreConfig.DEFAULT_RECALL_LIMIT)
        normalized_range = self._parse_time_range(time_range)
        normalized_kinds = self._normalize_kinds(kinds)
        normalized_allowed = tuple(self._normalize_sensitivity(item) for item in allowed_sensitivities)
        rows = await asyncio.to_thread(
            self._recall_memory_sync,
            str(query or "").strip(),
            subject_type,
            subject_key,
            normalized_range,
            normalized_kinds,
            normalized_allowed,
            max(normalized_limit * 5, normalized_limit),
        )
        ranked = self._rank_memory_rows(
            rows,
            query=str(query or ""),
            subject_type=subject_type,
            subject_key=subject_key,
        )
        selected = ranked[:normalized_limit]
        if selected:
            await asyncio.to_thread(self._mark_accessed_sync, [int(row["id"]) for row in selected])
        items = [self._render_memory_row(row, include_evidence=include_evidence) for row in selected]
        if include_evidence and items:
            await self._attach_evidence(items)
        return {
            "status": "ok",
            "items": items,
            "count": len(items),
            "max_items": normalized_limit,
        }

    def _recall_memory_sync(
        self,
        query: str,
        subject_type: Optional[str],
        subject_key: Optional[str],
        time_range: TimeRange,
        kinds: Optional[Sequence[str]],
        allowed_sensitivities: Sequence[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        where = ["mi.status = 'active'", self._placeholders("mi.sensitivity", allowed_sensitivities)]
        params: list[Any] = list(allowed_sensitivities)
        normalized_subject_type = self._normalize_subject_type(subject_type) if subject_type else None
        if normalized_subject_type:
            where.append("mi.subject_type = ?")
            params.append(normalized_subject_type)
        if subject_key:
            where.append("mi.subject_key = ?")
            params.append(str(subject_key))
        if kinds:
            where.append(self._placeholders("mi.kind", kinds))
            params.extend(kinds)
        if time_range.start is not None:
            where.append("COALESCE(mi.observed_at, mi.updated_at) >= ?")
            params.append(time_range.start)
        if time_range.end is not None:
            where.append("COALESCE(mi.observed_at, mi.updated_at) <= ?")
            params.append(time_range.end)
        where.append("(mi.valid_until IS NULL OR mi.valid_until >= ?)")
        params.append(time.time())
        where_sql = " AND ".join(f"({part})" for part in where)

        with self._connect() as connection:
            if query:
                try:
                    fts_query = self._fts_query(query)
                    rows = connection.execute(
                        f"""
                        SELECT mi.*, bm25(memory_items_fts) AS fts_rank
                        FROM memory_items_fts
                        JOIN memory_items mi ON mi.id = memory_items_fts.rowid
                        WHERE memory_items_fts MATCH ?
                        AND {where_sql}
                        LIMIT ?
                        """,
                        [fts_query, *params, limit],
                    ).fetchall()
                    if rows:
                        return [dict(row) for row in rows]
                except sqlite3.Error:
                    self.logger.debug("FTS memory recall failed; falling back to LIKE", exc_info=True)
                like = f"%{query}%"
                rows = connection.execute(
                    f"""
                    SELECT mi.*, 0.0 AS fts_rank
                    FROM memory_items mi
                    WHERE {where_sql}
                    AND (mi.title LIKE ? OR mi.content LIKE ? OR mi.subject_key LIKE ?)
                    ORDER BY mi.updated_at DESC
                    LIMIT ?
                    """,
                    [*params, like, like, like, limit],
                ).fetchall()
                return [dict(row) for row in rows]

            rows = connection.execute(
                f"""
                SELECT mi.*, 0.0 AS fts_rank
                FROM memory_items mi
                WHERE {where_sql}
                ORDER BY mi.updated_at DESC
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
            return [dict(row) for row in rows]

    async def search_history(
        self,
        query: str,
        *,
        time_range: Any = None,
        conversation_id: Optional[str] = None,
        speaker_id: Optional[str] = None,
        max_events: int = 20,
        allowed_sensitivities: Sequence[str] = (Sensitivity.NORMAL,),
    ) -> dict[str, Any]:
        """Search raw events for deep recall and exact history lookup."""
        normalized_limit = self._normalize_limit(max_events, default=20)
        normalized_range = self._parse_time_range(time_range)
        normalized_allowed = tuple(self._normalize_sensitivity(item) for item in allowed_sensitivities)
        rows = await asyncio.to_thread(
            self._search_history_sync,
            str(query or "").strip(),
            normalized_range,
            conversation_id,
            speaker_id,
            normalized_allowed,
            normalized_limit,
        )
        return {
            "status": "ok",
            "events": [self._render_event_row(row) for row in rows],
            "count": len(rows),
            "max_events": normalized_limit,
        }

    def _search_history_sync(
        self,
        query: str,
        time_range: TimeRange,
        conversation_id: Optional[str],
        speaker_id: Optional[str],
        allowed_sensitivities: Sequence[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        where = [self._placeholders("e.sensitivity", allowed_sensitivities)]
        params: list[Any] = list(allowed_sensitivities)
        if conversation_id:
            where.append("e.conversation_id = ?")
            params.append(conversation_id)
        if speaker_id:
            where.append("e.speaker_id = ?")
            params.append(speaker_id)
        if time_range.start is not None:
            where.append("e.timestamp_utc >= ?")
            params.append(time_range.start)
        if time_range.end is not None:
            where.append("e.timestamp_utc <= ?")
            params.append(time_range.end)
        where_sql = " AND ".join(f"({part})" for part in where)
        with self._connect() as connection:
            if query:
                try:
                    rows = connection.execute(
                        f"""
                        SELECT e.*, bm25(events_fts) AS fts_rank
                        FROM events_fts
                        JOIN events e ON e.id = events_fts.rowid
                        WHERE events_fts MATCH ?
                        AND {where_sql}
                        ORDER BY e.timestamp_utc DESC
                        LIMIT ?
                        """,
                        [self._fts_query(query), *params, limit],
                    ).fetchall()
                    if rows:
                        return [dict(row) for row in rows]
                except sqlite3.Error:
                    self.logger.debug("FTS history search failed; falling back to LIKE", exc_info=True)
                like = f"%{query}%"
                rows = connection.execute(
                    f"""
                    SELECT e.*, 0.0 AS fts_rank
                    FROM events e
                    WHERE {where_sql}
                    AND e.content LIKE ?
                    ORDER BY e.timestamp_utc DESC
                    LIMIT ?
                    """,
                    [*params, like, limit],
                ).fetchall()
                return [dict(row) for row in rows]
            rows = connection.execute(
                f"""
                SELECT e.*, 0.0 AS fts_rank
                FROM events e
                WHERE {where_sql}
                ORDER BY e.timestamp_utc DESC
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
            return [dict(row) for row in rows]

    async def correct_memory(
        self,
        *,
        memory_id: Optional[int] = None,
        query: Optional[str] = None,
        correction: str,
        reason: str,
        actor: str = "user",
    ) -> dict[str, Any]:
        """Correct an active memory item and preserve a revision record."""
        normalized_correction = str(correction or "").strip()
        if not normalized_correction:
            return {"status": "skipped", "message": "Correction cannot be empty."}
        target_id = memory_id or await self._resolve_memory_id(query)
        if not target_id:
            return {"status": "not_found", "message": "No matching memory found."}
        async with self._write_lock:
            changed = await asyncio.to_thread(
                self._correct_memory_sync,
                int(target_id),
                normalized_correction,
                str(reason or "correction").strip(),
                str(actor or "user").strip(),
            )
        return {"status": "ok" if changed else "not_found", "memory_id": int(target_id)}

    def _correct_memory_sync(self, memory_id: int, correction: str, reason: str, actor: str) -> bool:
        now = time.time()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT content FROM memory_items WHERE id = ?",
                (memory_id,),
            ).fetchone()
            if row is None:
                return False
            old_content = str(row["content"])
            connection.execute(
                """
                UPDATE memory_items
                SET content = ?, updated_at = ?, confidence = MAX(confidence, 0.9), status = 'active'
                WHERE id = ?
                """,
                (correction, now, memory_id),
            )
            connection.execute(
                """
                INSERT INTO memory_revisions (
                    memory_id, revision_type, old_content, new_content, reason, actor, created_at
                )
                VALUES (?, 'correction', ?, ?, ?, ?, ?)
                """,
                (memory_id, old_content, correction, reason, actor, now),
            )
            connection.commit()
            return True

    async def forget_memory(
        self,
        *,
        memory_id: Optional[int] = None,
        query: Optional[str] = None,
        mode: str = "archive",
        reason: str = "forget requested",
        actor: str = "user",
    ) -> dict[str, Any]:
        """Archive or delete a memory item."""
        target_id = memory_id or await self._resolve_memory_id(query)
        if not target_id:
            return {"status": "not_found", "message": "No matching memory found."}
        normalized_mode = str(mode or "archive").strip().lower()
        if normalized_mode not in {"archive", "delete"}:
            normalized_mode = "archive"
        async with self._write_lock:
            changed = await asyncio.to_thread(
                self._forget_memory_sync,
                int(target_id),
                normalized_mode,
                str(reason or "forget requested"),
                str(actor or "user"),
            )
        return {"status": "ok" if changed else "not_found", "memory_id": int(target_id), "mode": normalized_mode}

    def _forget_memory_sync(self, memory_id: int, mode: str, reason: str, actor: str) -> bool:
        now = time.time()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT content FROM memory_items WHERE id = ?",
                (memory_id,),
            ).fetchone()
            if row is None:
                return False
            old_content = str(row["content"])
            connection.execute(
                """
                INSERT INTO memory_revisions (
                    memory_id, revision_type, old_content, new_content, reason, actor, created_at
                )
                VALUES (?, ?, ?, NULL, ?, ?, ?)
                """,
                (memory_id, "delete" if mode == "delete" else "archive", old_content, reason, actor, now),
            )
            if mode == "delete":
                connection.execute("DELETE FROM memory_evidence WHERE memory_id = ?", (memory_id,))
                connection.execute("DELETE FROM memory_items WHERE id = ?", (memory_id,))
            else:
                connection.execute(
                    "UPDATE memory_items SET status = 'archived', updated_at = ? WHERE id = ?",
                    (now, memory_id),
                )
            connection.commit()
            return True

    async def list_memory_items(
        self,
        *,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        subject_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        normalized_limit = self._normalize_limit(limit, default=50)
        normalized_offset = max(0, int(offset or 0))
        rows = await asyncio.to_thread(
            self._list_memory_items_sync,
            status,
            kind,
            subject_type,
            normalized_limit,
            normalized_offset,
        )
        return {
            "status": "ok",
            "items": [self._render_memory_row(row) for row in rows],
            "count": len(rows),
            "limit": normalized_limit,
            "offset": normalized_offset,
        }

    async def summary_exists(
        self,
        *,
        summary_type: str,
        period_start: float | str | date,
        period_end: float | str | date,
        scope_type: str = SubjectType.SELF,
        scope_key: str = "self",
    ) -> bool:
        start = self._coerce_time(period_start)
        end = self._coerce_time(period_end)
        return await asyncio.to_thread(
            self._summary_exists_sync,
            summary_type,
            start,
            end,
            scope_type,
            scope_key,
        )

    def _summary_exists_sync(
        self,
        summary_type: str,
        period_start: Optional[float],
        period_end: Optional[float],
        scope_type: str,
        scope_key: str,
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM memory_summaries
                WHERE summary_type = ?
                AND scope_type = ?
                AND scope_key = ?
                AND period_start IS ?
                AND period_end IS ?
                LIMIT 1
                """,
                (summary_type, scope_type, scope_key, period_start, period_end),
            ).fetchone()
            return row is not None

    async def add_summary(
        self,
        *,
        summary_type: str,
        scope_type: str,
        scope_key: str,
        period_start: float | str | date,
        period_end: float | str | date,
        content: str,
        source_memory_ids: Optional[Sequence[int]] = None,
    ) -> int:
        normalized_content = str(content or "").strip()
        if not normalized_content:
            return 0
        start = self._coerce_time(period_start)
        end = self._coerce_time(period_end)
        source_ids = [int(item) for item in source_memory_ids or []]
        async with self._write_lock:
            summary_id = await asyncio.to_thread(
                self._add_summary_sync,
                summary_type,
                scope_type,
                scope_key,
                start,
                end,
                normalized_content,
                source_ids,
            )
        await self.remember(
            content=normalized_content,
            kind=MemoryKind.SUMMARY,
            subject_type=scope_type,
            subject_key=scope_key,
            title=f"{summary_type} summary",
            salience=0.75,
            confidence=0.8,
            observed_at=end,
            metadata={
                "summary_id": summary_id,
                "summary_type": summary_type,
                "period_start": start,
                "period_end": end,
                "source_memory_ids": source_ids,
            },
        )
        return summary_id

    def _add_summary_sync(
        self,
        summary_type: str,
        scope_type: str,
        scope_key: str,
        period_start: Optional[float],
        period_end: Optional[float],
        content: str,
        source_memory_ids: Sequence[int],
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO memory_summaries (
                    summary_type, scope_type, scope_key, period_start, period_end,
                    content, source_memory_ids_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    summary_type,
                    scope_type,
                    scope_key,
                    period_start,
                    period_end,
                    content,
                    json.dumps(list(source_memory_ids), ensure_ascii=False),
                    time.time(),
                ),
            )
            connection.commit()
            return int(cursor.lastrowid or 0)

    def _list_memory_items_sync(
        self,
        status: Optional[str],
        kind: Optional[str],
        subject_type: Optional[str],
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("status = ?")
            params.append(self._normalize_status(status))
        if kind:
            where.append("kind = ?")
            params.append(self._normalize_kind(kind))
        if subject_type:
            where.append("subject_type = ?")
            params.append(self._normalize_subject_type(subject_type))
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM memory_items
                {where_sql}
                ORDER BY updated_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
            return [dict(row) for row in rows]

    async def get_memory_item(self, memory_id: int, *, include_evidence: bool = True) -> Optional[dict[str, Any]]:
        row = await asyncio.to_thread(self._get_memory_item_sync, int(memory_id))
        if row is None:
            return None
        item = self._render_memory_row(row, include_evidence=False)
        if include_evidence:
            await self._attach_evidence([item])
        return item

    def _get_memory_item_sync(self, memory_id: int) -> Optional[dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM memory_items WHERE id = ?",
                (memory_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    async def get_events(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        speaker_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ) -> dict[str, Any]:
        normalized_limit = self._normalize_limit(limit, default=50)
        normalized_offset = max(0, int(offset or 0))
        rows = await asyncio.to_thread(
            self._get_events_sync,
            normalized_limit,
            normalized_offset,
            speaker_id,
            conversation_id,
        )
        return {
            "status": "ok",
            "events": [self._render_event_row(row) for row in rows],
            "count": len(rows),
            "limit": normalized_limit,
            "offset": normalized_offset,
        }

    async def get_event(self, event_id: int) -> Optional[dict[str, Any]]:
        row = await asyncio.to_thread(self._get_event_sync, int(event_id))
        return self._render_event_row(row) if row is not None else None

    def _get_event_sync(self, event_id: int) -> Optional[dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM events WHERE id = ?",
                (event_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def _get_events_sync(
        self,
        limit: int,
        offset: int,
        speaker_id: Optional[str],
        conversation_id: Optional[str],
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if speaker_id:
            where.append("speaker_id = ?")
            params.append(speaker_id)
        if conversation_id:
            where.append("conversation_id = ?")
            params.append(conversation_id)
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM events
                {where_sql}
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
            return [dict(row) for row in rows]

    async def clear(self) -> None:
        """Clear all events, memories, evidence, revisions, people, summaries, and policies."""
        async with self._write_lock:
            await asyncio.to_thread(self._clear_all_sync)

    def _clear_all_sync(self) -> None:
        with self._connect() as connection:
            for table in (
                "memory_evidence",
                "memory_revisions",
                "memory_summaries",
                "retention_policies",
                "people",
                "memory_items",
                "events",
            ):
                connection.execute(f"DELETE FROM {table}")
            connection.commit()

    async def get_stats(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_stats_sync)

    def _get_stats_sync(self) -> dict[str, Any]:
        with self._connect() as connection:
            stats = {
                "path": str(self.path),
                "schema_version": ExperienceMemoryStoreConfig.SCHEMA_VERSION,
                "events": self._count_table_with_connection(connection, "events"),
                "memory_items": self._count_table_with_connection(connection, "memory_items"),
                "active_memory_items": self._count_where_with_connection(
                    connection,
                    "memory_items",
                    "status = 'active'",
                ),
                "candidate_memory_items": self._count_where_with_connection(
                    connection,
                    "memory_items",
                    "status = 'candidate'",
                ),
                "evidence": self._count_table_with_connection(connection, "memory_evidence"),
                "revisions": self._count_table_with_connection(connection, "memory_revisions"),
                "people": self._count_table_with_connection(connection, "people"),
                "summaries": self._count_table_with_connection(connection, "memory_summaries"),
            }
            row = connection.execute(
                "SELECT MIN(timestamp_utc) AS earliest, MAX(timestamp_utc) AS latest FROM events"
            ).fetchone()
            if row is not None:
                stats["earliest_event"] = row["earliest"]
                stats["latest_event"] = row["latest"]
            return stats

    async def export_memory(self, export_dir: str | Path) -> dict[str, Any]:
        """Export active memories and recent events as Markdown and JSONL."""
        target = Path(export_dir).expanduser()
        rows = await asyncio.to_thread(self._export_rows_sync)
        await asyncio.to_thread(self._write_export_sync, target, rows)
        return {
            "status": "ok",
            "path": str(target),
            "memory_items": len(rows["memory_items"]),
            "events": len(rows["events"]),
        }

    def _export_rows_sync(self) -> dict[str, list[dict[str, Any]]]:
        with self._connect() as connection:
            memory_rows = connection.execute(
                """
                SELECT *
                FROM memory_items
                WHERE status = 'active'
                ORDER BY subject_type, subject_key, kind, updated_at DESC
                """
            ).fetchall()
            event_rows = connection.execute(
                """
                SELECT *
                FROM events
                ORDER BY timestamp_utc DESC
                LIMIT 1000
                """
            ).fetchall()
            return {
                "memory_items": [dict(row) for row in memory_rows],
                "events": [dict(row) for row in event_rows],
            }

    def _write_export_sync(self, target: Path, rows: Mapping[str, list[dict[str, Any]]]) -> None:
        today_dir = target / datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_dir.mkdir(parents=True, exist_ok=True)
        memory_items = rows.get("memory_items", [])
        events = rows.get("events", [])
        markdown_lines = ["# xAgent Memory Export", ""]
        current_subject = None
        for item in memory_items:
            subject = f"{item['subject_type']}:{item['subject_key']}"
            if subject != current_subject:
                markdown_lines.extend(["", f"## {subject}", ""])
                current_subject = subject
            markdown_lines.extend([
                f"### [{item['kind']}] {item['title']}",
                "",
                str(item["content"]).strip(),
                "",
                f"- memory_id: {item['id']}",
                f"- confidence: {item['confidence']}",
                f"- salience: {item['salience']}",
                "",
            ])
        (today_dir / "memories.md").write_text("\n".join(markdown_lines).strip() + "\n", encoding="utf-8")
        jsonl_path = target / "memory.jsonl"
        with jsonl_path.open("w", encoding="utf-8") as handle:
            for item in memory_items:
                handle.write(json.dumps({"type": "memory", **self._json_ready(item)}, ensure_ascii=False) + "\n")
            for event in events:
                handle.write(json.dumps({"type": "event", **self._json_ready(event)}, ensure_ascii=False) + "\n")

    async def query_sql(self, sql: str, max_rows: int = 50) -> dict[str, Any]:
        """Internal/debug read-only SQL entrypoint."""
        return await asyncio.to_thread(execute_readonly_query, self.path, sql, max_rows=max_rows)

    def get_stream_info(self) -> dict[str, str]:
        return {
            "stream": "experience_memory",
            "backend": "sqlite",
            "path": str(self.path),
        }

    @staticmethod
    def normalize_messages(messages: MessageBatch) -> list[Message]:
        if isinstance(messages, Message):
            return [messages]
        normalized = list(messages)
        if not all(isinstance(message, Message) for message in normalized):
            raise TypeError("messages must be a Message or a sequence of Message instances")
        return normalized

    @staticmethod
    def validate_pagination(count: int, offset: int = 0) -> tuple[int, int]:
        try:
            normalized_count = int(count)
            normalized_offset = int(offset)
        except (TypeError, ValueError) as exception:
            raise ValueError("count and offset must be integers") from exception
        if normalized_count <= 0:
            raise ValueError("count must be a positive integer")
        if normalized_offset < 0:
            raise ValueError("offset must be a non-negative integer")
        return normalized_count, normalized_offset

    async def _resolve_memory_id(self, query: Optional[str]) -> Optional[int]:
        if not query:
            return None
        result = await self.recall_memory(query=query, max_items=1)
        items = result.get("items", [])
        if not items:
            return None
        return int(items[0]["memory_id"])

    def _insert_evidence_sync(
        self,
        connection: sqlite3.Connection,
        *,
        memory_id: int,
        event_ids: Sequence[int],
        quote: str,
        relation: str,
        confidence: float,
        extractor_model: Optional[str],
    ) -> None:
        if not memory_id:
            return
        now = time.time()
        normalized_quote = str(quote or "").strip()
        if event_ids:
            for event_id in event_ids:
                connection.execute(
                    """
                    INSERT INTO memory_evidence (
                        memory_id, event_id, quote, relation, confidence, extractor_model, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (memory_id, int(event_id), normalized_quote, relation, confidence, extractor_model, now),
                )
            return
        if normalized_quote:
            connection.execute(
                """
                INSERT INTO memory_evidence (
                    memory_id, event_id, quote, relation, confidence, extractor_model, created_at
                )
                VALUES (?, NULL, ?, ?, ?, ?, ?)
                """,
                (memory_id, normalized_quote, relation, confidence, extractor_model, now),
            )

    def _upsert_person_sync(
        self,
        connection: sqlite3.Connection,
        *,
        person_key: str,
        display_name: str,
    ) -> None:
        now = time.time()
        connection.execute(
            """
            INSERT INTO people (person_key, display_name, aliases_json, created_at, updated_at)
            VALUES (?, ?, '[]', ?, ?)
            ON CONFLICT(person_key) DO UPDATE SET
                display_name = excluded.display_name,
                updated_at = excluded.updated_at
            """,
            (person_key, display_name or person_key, now, now),
        )

    async def _attach_evidence(self, items: list[dict[str, Any]]) -> None:
        evidence = await asyncio.to_thread(
            self._evidence_for_memory_ids_sync,
            [int(item["memory_id"]) for item in items],
        )
        by_memory: dict[int, list[dict[str, Any]]] = {}
        for row in evidence:
            by_memory.setdefault(int(row["memory_id"]), []).append(row)
        for item in items:
            item["evidence"] = by_memory.get(int(item["memory_id"]), [])

    def _evidence_for_memory_ids_sync(self, memory_ids: Sequence[int]) -> list[dict[str, Any]]:
        if not memory_ids:
            return []
        placeholders = ", ".join("?" for _ in memory_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, memory_id, event_id, quote, relation, confidence, extractor_model, created_at
                FROM memory_evidence
                WHERE memory_id IN ({placeholders})
                ORDER BY created_at DESC, id DESC
                """,
                list(memory_ids),
            ).fetchall()
            return [dict(row) for row in rows]

    def _mark_accessed_sync(self, memory_ids: Sequence[int]) -> None:
        if not memory_ids:
            return
        placeholders = ", ".join("?" for _ in memory_ids)
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                f"""
                UPDATE memory_items
                SET last_accessed_at = ?, access_count = access_count + 1
                WHERE id IN ({placeholders})
                """,
                [now, *memory_ids],
            )
            connection.commit()

    def _rank_memory_rows(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        query: str,
        subject_type: Optional[str],
        subject_key: Optional[str],
    ) -> list[dict[str, Any]]:
        now = time.time()
        ranked: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            relevance = self._relevance_score(item, query)
            subject_match = self._subject_match_score(item, subject_type, subject_key, query)
            salience = self._clamp01(item.get("salience", 0.0))
            confidence = self._clamp01(item.get("confidence", 0.0))
            observed_or_updated = float(item.get("observed_at") or item.get("updated_at") or now)
            age_days = max(0.0, (now - observed_or_updated) / 86400.0)
            recency = 1.0 / (1.0 + age_days / 30.0)
            access_count = max(0, int(item.get("access_count") or 0))
            access_signal = min(1.0, access_count / 20.0)
            score = (
                relevance * 0.45
                + subject_match * 0.20
                + salience * 0.15
                + confidence * 0.10
                + recency * 0.07
                + access_signal * 0.03
            )
            item["score"] = round(score, 6)
            ranked.append(item)
        return sorted(ranked, key=lambda item: (float(item["score"]), float(item.get("updated_at") or 0)), reverse=True)

    @staticmethod
    def _relevance_score(row: Mapping[str, Any], query: str) -> float:
        if not query.strip():
            return 0.35
        rank = row.get("fts_rank")
        if isinstance(rank, (int, float)):
            return 1.0 / (1.0 + abs(float(rank)))
        haystack = f"{row.get('title', '')} {row.get('content', '')} {row.get('subject_key', '')}".casefold()
        terms = [term.casefold() for term in query.split() if term.strip()]
        if not terms:
            return 0.35
        hits = sum(1 for term in terms if term in haystack)
        return min(1.0, hits / len(terms))

    def _subject_match_score(
        self,
        row: Mapping[str, Any],
        subject_type: Optional[str],
        subject_key: Optional[str],
        query: str,
    ) -> float:
        row_type = str(row.get("subject_type") or "")
        row_key = str(row.get("subject_key") or "")
        if subject_key and row_key == str(subject_key):
            return 1.0
        if subject_type and row_type == self._normalize_subject_type(subject_type):
            return 0.7
        if query and row_key and row_key.casefold() in query.casefold():
            return 0.5
        return 0.25

    def _message_from_event_row(self, row: sqlite3.Row | Mapping[str, Any]) -> Optional[Message]:
        data = dict(row)
        metadata = self._json_loads(data.get("metadata_json"))
        message_json = metadata.get("message_json")
        if isinstance(message_json, Mapping):
            try:
                message = Message.model_validate(message_json)
                message.metadata.setdefault("event_id", data["id"])
                return message
            except Exception as exc:
                self.logger.warning("Skipping invalid event message JSON: %s", exc)
        try:
            role = RoleType(data["role"])
        except Exception:
            role = RoleType.USER
        try:
            message_type = MessageType(data["event_type"])
        except Exception:
            message_type = MessageType.Message
        return Message(
            role=role,
            type=message_type,
            sender_id=data.get("speaker_id"),
            content=str(data.get("content") or ""),
            timestamp=float(data.get("timestamp_utc") or time.time()),
            metadata={key: value for key, value in metadata.items() if key != "message_json"} | {"event_id": data["id"]},
        )

    def _render_memory_row(self, row: Mapping[str, Any], *, include_evidence: bool = False) -> dict[str, Any]:
        item = {
            "memory_id": int(row["id"]),
            "kind": row["kind"],
            "subject": {
                "type": row["subject_type"],
                "key": row["subject_key"],
            },
            "title": row["title"],
            "content": row["content"],
            "status": row["status"],
            "salience": float(row["salience"]),
            "confidence": float(row["confidence"]),
            "sensitivity": row["sensitivity"],
            "valid_from": row["valid_from"],
            "valid_until": row["valid_until"],
            "observed_at": row["observed_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_accessed_at": row["last_accessed_at"],
            "access_count": int(row["access_count"] or 0),
            "score": row.get("score"),
            "metadata": self._json_loads(row.get("metadata_json")),
        }
        if include_evidence:
            item["evidence"] = []
        return item

    def _render_event_row(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "event_id": int(row["id"]),
            "timestamp_utc": float(row["timestamp_utc"]),
            "timezone": row["timezone"],
            "channel": row["channel"],
            "conversation_id": row["conversation_id"],
            "room_id": row["room_id"],
            "speaker_id": row["speaker_id"],
            "role": row["role"],
            "event_type": row["event_type"],
            "content": row["content"],
            "privacy_scope": row["privacy_scope"],
            "sensitivity": row["sensitivity"],
            "metadata": {
                key: value
                for key, value in self._json_loads(row.get("metadata_json")).items()
                if key != "message_json"
            },
        }

    def _count_table_sync(self, table_name: str) -> int:
        with self._connect() as connection:
            return self._count_table_with_connection(connection, table_name)

    @staticmethod
    def current_week_range() -> tuple[date, date]:
        today = date.today()
        monday = today - date.resolution * today.weekday()
        sunday = monday + date.resolution * 6
        return monday, sunday

    @staticmethod
    def week_range_for(target_date: date) -> tuple[date, date]:
        monday = target_date - date.resolution * target_date.weekday()
        sunday = monday + date.resolution * 6
        return monday, sunday

    @staticmethod
    def _count_table_with_connection(connection: sqlite3.Connection, table_name: str) -> int:
        row = connection.execute(f"SELECT COUNT(*) AS row_count FROM {table_name}").fetchone()
        return int(row["row_count"]) if row is not None else 0

    @staticmethod
    def _count_where_with_connection(connection: sqlite3.Connection, table_name: str, where: str) -> int:
        row = connection.execute(f"SELECT COUNT(*) AS row_count FROM {table_name} WHERE {where}").fetchone()
        return int(row["row_count"]) if row is not None else 0

    @staticmethod
    def _normalize_kind(value: Optional[str]) -> str:
        normalized = str(value or MemoryKind.SEMANTIC_FACT).strip()
        return normalized if normalized in MemoryKind.VALUES else MemoryKind.SEMANTIC_FACT

    @staticmethod
    def _normalize_subject_type(value: Optional[str]) -> str:
        normalized = str(value or SubjectType.SELF).strip()
        return normalized if normalized in SubjectType.VALUES else SubjectType.TOPIC

    @staticmethod
    def _normalize_status(value: Optional[str]) -> str:
        normalized = str(value or MemoryStatus.ACTIVE).strip()
        return normalized if normalized in MemoryStatus.VALUES else MemoryStatus.ACTIVE

    @staticmethod
    def _normalize_sensitivity(value: Optional[str]) -> str:
        normalized = str(value or Sensitivity.NORMAL).strip()
        return normalized if normalized in Sensitivity.VALUES else Sensitivity.NORMAL

    @classmethod
    def _normalize_kinds(cls, kinds: Optional[Sequence[str] | str]) -> Optional[list[str]]:
        if not kinds:
            return None
        if isinstance(kinds, str):
            values = [part.strip() for part in kinds.split(",")]
        else:
            values = [str(part).strip() for part in kinds]
        normalized = [cls._normalize_kind(value) for value in values if value]
        return normalized or None

    @staticmethod
    def _normalize_limit(value: int, *, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(1, min(parsed, ExperienceMemoryStoreConfig.HARD_LIMIT))

    @staticmethod
    def _clamp01(value: Any) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = 0.0
        if math.isnan(parsed):
            return 0.0
        return max(0.0, min(parsed, 1.0))

    @staticmethod
    def _default_title(kind: str, content: str) -> str:
        normalized = " ".join(content.split())
        return f"{kind}: {normalized[:80]}" if normalized else kind

    @staticmethod
    def _content_hash(content: str) -> str:
        return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()

    @staticmethod
    def _json_dumps(payload: Mapping[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)

    @staticmethod
    def _json_loads(value: Any) -> dict[str, Any]:
        if not value:
            return {}
        if isinstance(value, Mapping):
            return dict(value)
        try:
            parsed = json.loads(str(value))
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _json_ready(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: ExperienceMemoryStore._json_loads(value) if key.endswith("_json") else value
            for key, value in row.items()
        }

    @staticmethod
    def _placeholders(column: str, values: Sequence[Any]) -> str:
        placeholders = ", ".join("?" for _ in values) or "?"
        return f"{column} IN ({placeholders})"

    @staticmethod
    def _fts_query(query: str) -> str:
        terms = [term.replace('"', '""') for term in str(query or "").split() if term.strip()]
        if not terms:
            return '""'
        return " OR ".join(f'"{term}"' for term in terms)

    @classmethod
    def _parse_time_range(cls, value: Any) -> TimeRange:
        if value is None or value == "":
            return TimeRange()
        if isinstance(value, TimeRange):
            return value
        if isinstance(value, Mapping):
            return TimeRange(
                start=cls._coerce_time(value.get("start") or value.get("from")),
                end=cls._coerce_time(value.get("end") or value.get("to")),
            )
        if isinstance(value, (tuple, list)) and len(value) >= 2:
            start = cls._coerce_time(value[0])
            end = cls._coerce_time(value[1])
            if isinstance(value[1], date) and not isinstance(value[1], datetime) and end is not None:
                end += 86399
            return TimeRange(start=start, end=end)
        if isinstance(value, str):
            text = value.strip()
            for separator in (" to ", "..", ","):
                if separator in text:
                    start, end = text.split(separator, 1)
                    end_value = cls._coerce_time(end)
                    if len(end.strip()) <= 10 and end_value is not None:
                        end_value += 86399
                    return TimeRange(start=cls._coerce_time(start), end=end_value)
            single = cls._coerce_time(text)
            if single is not None:
                return TimeRange(start=single, end=single + 86399 if len(text) <= 10 else single)
        return TimeRange()

    @staticmethod
    def _coerce_time(value: Any) -> Optional[float]:
        if value is None or value == "":
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, date) and not isinstance(value, datetime):
            return datetime(value.year, value.month, value.day, tzinfo=timezone.utc).timestamp()
        if isinstance(value, datetime):
            dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        text = str(value).strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            pass
        try:
            if len(text) <= 10:
                return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None

    def __repr__(self) -> str:
        return f"ExperienceMemoryStore(path='{self.path}')"
