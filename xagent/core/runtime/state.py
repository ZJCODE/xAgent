"""Clean, versioned SQLite state for one xAgent runtime."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from ..database import STATE_SCHEMA_VERSION, initialize_state_database
from .types import (
    DELIVERY_STATUS_BLOCKED,
    DELIVERY_STATUS_DELIVERED,
    DELIVERY_STATUS_FAILED,
    DELIVERY_STATUS_PENDING,
    DELIVERY_STATUS_SENDING,
    DELIVERY_STATUS_UNKNOWN,
    EVENT_STATUS_COMPLETED,
    EVENT_STATUS_FAILED,
    EVENT_STATUS_NEEDS_REVIEW,
    EVENT_STATUS_PENDING,
    EVENT_STATUS_PROCESSING,
    LOCAL_OWNER_PERSON_ID,
    MAX_PENDING_EVENTS_PER_SOURCE,
    AgentEvent,
    Delivery,
    RuntimeBacklogFull,
    StoredEvent,
)


RUNTIME_SCHEMA_VERSION = STATE_SCHEMA_VERSION
_EVENT_STATUSES = {
    EVENT_STATUS_PENDING,
    EVENT_STATUS_PROCESSING,
    EVENT_STATUS_COMPLETED,
    EVENT_STATUS_FAILED,
    EVENT_STATUS_NEEDS_REVIEW,
}
_DELIVERY_STATUSES = {
    DELIVERY_STATUS_PENDING,
    DELIVERY_STATUS_SENDING,
    DELIVERY_STATUS_DELIVERED,
    DELIVERY_STATUS_BLOCKED,
    DELIVERY_STATUS_FAILED,
    DELIVERY_STATUS_UNKNOWN,
}


class RuntimeStateStore:
    """Durable FIFO and operational state owned by one runtime."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_pending_per_source: int = MAX_PENDING_EVENTS_PER_SOURCE,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_pending_per_source = max(1, int(max_pending_per_source))
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        initialize_state_database(self.path)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE deliveries
                SET status=?, error='runtime interrupted while sending', updated_at=?
                WHERE status=?
                """,
                (DELIVERY_STATUS_UNKNOWN, time.time(), DELIVERY_STATUS_SENDING),
            )
            now = time.time()
            connection.execute(
                """
                INSERT INTO people(person_id, display_name, created_at, last_seen_at)
                VALUES (?, 'You', ?, ?)
                ON CONFLICT(person_id) DO UPDATE SET last_seen_at=excluded.last_seen_at
                """,
                (LOCAL_OWNER_PERSON_ID, now, now),
            )
            connection.commit()

    async def enqueue_event(self, event: AgentEvent) -> tuple[int, bool]:
        return await asyncio.to_thread(self._enqueue_event_sync, event)

    def _enqueue_event_sync(self, event: AgentEvent) -> tuple[int, bool]:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM runtime_events WHERE event_id=?",
                (event.event_id,),
            ).fetchone()
            if existing is not None:
                stored = self._stored_event(existing)
                if stored.event != event:
                    connection.rollback()
                    raise ValueError(
                        f"event_id collision with different payload: {event.event_id}"
                    )
                connection.commit()
                return int(existing["sequence"]), False
            count_row = connection.execute(
                """
                SELECT COUNT(*) AS pending_count
                FROM runtime_events
                WHERE source=? AND status IN (?, ?)
                """,
                (event.source, EVENT_STATUS_PENDING, EVENT_STATUS_PROCESSING),
            ).fetchone()
            if int(count_row["pending_count"]) >= self.max_pending_per_source:
                connection.rollback()
                raise RuntimeBacklogFull(
                    f"source {event.source!r} already has "
                    f"{self.max_pending_per_source} pending events"
                )
            cursor = connection.execute(
                """
                INSERT INTO runtime_events(
                    event_id, kind, source, conversation_id, speaker_id,
                    audience_json, content, event_timestamp, metadata_json,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.kind,
                    event.source,
                    event.conversation_id,
                    event.speaker_id,
                    json.dumps(list(event.audience_ids), ensure_ascii=False),
                    event.content,
                    event.timestamp,
                    json.dumps(event.metadata, ensure_ascii=False),
                    EVENT_STATUS_PENDING,
                    now,
                    now,
                ),
            )
            connection.commit()
            return int(cursor.lastrowid), True

    async def recover_interrupted(self) -> tuple[int, int]:
        return await asyncio.to_thread(self._recover_interrupted_sync)

    def _recover_interrupted_sync(self) -> tuple[int, int]:
        now = time.time()
        with self._connect() as connection:
            retryable = connection.execute(
                """
                UPDATE runtime_events
                SET status=?, updated_at=?, error=''
                WHERE status=? AND side_effect_started=0
                """,
                (EVENT_STATUS_PENDING, now, EVENT_STATUS_PROCESSING),
            ).rowcount
            needs_review = connection.execute(
                """
                UPDATE runtime_events
                SET status=?, updated_at=?,
                    error='runtime stopped after a potentially mutating tool started'
                WHERE status=? AND side_effect_started=1
                """,
                (EVENT_STATUS_NEEDS_REVIEW, now, EVENT_STATUS_PROCESSING),
            ).rowcount
            connection.commit()
            return int(retryable), int(needs_review)

    async def claim_next_event(self) -> StoredEvent | None:
        return await asyncio.to_thread(self._claim_next_event_sync)

    def _claim_next_event_sync(self) -> StoredEvent | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM runtime_events
                WHERE status=?
                ORDER BY sequence ASC
                LIMIT 1
                """,
                (EVENT_STATUS_PENDING,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            updated = connection.execute(
                """
                UPDATE runtime_events SET status=?, updated_at=?
                WHERE sequence=? AND status=?
                """,
                (
                    EVENT_STATUS_PROCESSING,
                    time.time(),
                    row["sequence"],
                    EVENT_STATUS_PENDING,
                ),
            ).rowcount
            connection.commit()
            if updated != 1:
                return None
            values = dict(row)
            values["status"] = EVENT_STATUS_PROCESSING
            return self._stored_event(values)

    async def mark_side_effect_started(self, event_id: str) -> None:
        await asyncio.to_thread(
            self._update_event_sync,
            event_id,
            {"side_effect_started": 1, "updated_at": time.time()},
        )

    async def complete_event(self, event_id: str, result: dict[str, Any]) -> None:
        await asyncio.to_thread(
            self._update_event_sync,
            event_id,
            {
                "status": EVENT_STATUS_COMPLETED,
                "result_json": json.dumps(result, ensure_ascii=False),
                "error": "",
                "updated_at": time.time(),
            },
        )

    async def fail_event(self, event_id: str, error: str) -> None:
        await asyncio.to_thread(
            self._fail_event_sync,
            event_id,
            str(error),
        )

    def _fail_event_sync(self, event_id: str, error: str) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE runtime_events
                SET status=?, error=?, updated_at=?
                WHERE event_id=?
                """,
                (EVENT_STATUS_FAILED, error, now, event_id),
            ).rowcount
            if updated != 1:
                connection.rollback()
                raise KeyError(f"unknown runtime event: {event_id}")
            connection.execute(
                """
                UPDATE deliveries
                SET status=?, error=?, updated_at=?
                WHERE event_id=? AND status=?
                """,
                (
                    DELIVERY_STATUS_FAILED,
                    f"source event failed: {error}",
                    now,
                    event_id,
                    DELIVERY_STATUS_PENDING,
                ),
            )
            connection.commit()

    def _update_event_sync(self, event_id: str, values: dict[str, Any]) -> None:
        allowed = {"status", "side_effect_started", "result_json", "error", "updated_at"}
        if not values or set(values) - allowed:
            raise ValueError("unsupported runtime event update")
        if "status" in values and values["status"] not in _EVENT_STATUSES:
            raise ValueError(f"invalid event status: {values['status']}")
        assignments = ", ".join(f"{name}=?" for name in values)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE runtime_events SET {assignments} WHERE event_id=?",
                (*values.values(), event_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown runtime event: {event_id}")
            connection.commit()

    async def get_event(self, event_id: str) -> StoredEvent | None:
        return await asyncio.to_thread(self._get_event_sync, event_id)

    def _get_event_sync(self, event_id: str) -> StoredEvent | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
        return self._stored_event(row) if row is not None else None

    async def list_events(self, *, status: str | None = None) -> list[StoredEvent]:
        return await asyncio.to_thread(self._list_events_sync, status)

    def _list_events_sync(self, status: str | None) -> list[StoredEvent]:
        query = "SELECT * FROM runtime_events"
        params: tuple[Any, ...] = ()
        if status is not None:
            if status not in _EVENT_STATUSES:
                raise ValueError(f"invalid event status: {status}")
            query += " WHERE status=?"
            params = (status,)
        query += " ORDER BY sequence ASC"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._stored_event(row) for row in rows]

    async def list_messages(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        query: str = "",
        role: str = "",
        source: str = "",
    ) -> dict[str, Any]:
        """Return one bounded page from the durable conversational timeline."""
        return await asyncio.to_thread(
            self._list_messages_sync,
            limit,
            offset,
            query,
            role,
            source,
        )

    def _list_messages_sync(
        self,
        limit: int,
        offset: int,
        query: str,
        role: str,
        source: str,
    ) -> dict[str, Any]:
        normalized_limit = int(limit)
        normalized_offset = int(offset)
        normalized_query = str(query or "").strip()
        normalized_role = str(role or "").strip().lower()
        normalized_source = str(source or "").strip().lower()
        if not 1 <= normalized_limit <= 100:
            raise ValueError("message limit must be between 1 and 100")
        if not 0 <= normalized_offset <= 100_000:
            raise ValueError("message offset must be between 0 and 100000")
        if len(normalized_query) > 256:
            raise ValueError("message search query must not exceed 256 characters")
        if normalized_role and normalized_role not in {
            "user",
            "assistant",
            "environment",
        }:
            raise ValueError(f"unknown message role: {normalized_role}")
        if len(normalized_source) > 64:
            raise ValueError("message source must not exceed 64 characters")

        conditions: list[str] = []
        params: list[Any] = []
        if normalized_query:
            escaped = (
                normalized_query
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            conditions.append(
                "json_extract(message_json, '$.content') LIKE ? ESCAPE '\\'"
            )
            params.append(f"%{escaped}%")
        if normalized_role:
            conditions.append("json_extract(message_json, '$.role')=?")
            params.append(normalized_role)
        if normalized_source:
            conditions.append("json_extract(message_json, '$.source')=?")
            params.append(normalized_source)

        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._connect() as connection:
            count_row = connection.execute(
                f"SELECT COUNT(*) AS total FROM messages{where}",
                tuple(params),
            ).fetchone()
            rows = connection.execute(
                f"""
                SELECT id, timestamp, message_json
                FROM messages
                {where}
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (*params, normalized_limit, normalized_offset),
            ).fetchall()

        messages: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = dict(json.loads(row["message_json"]))
            except (TypeError, ValueError):
                continue
            payload["id"] = int(row["id"])
            payload["timestamp"] = float(row["timestamp"])
            images = payload.get("images")
            if isinstance(images, list):
                payload["images"] = [
                    {"format": str(image.get("format") or "")}
                    for image in images[:10]
                    if isinstance(image, dict)
                ]
            metadata = payload.get("metadata")
            if isinstance(metadata, dict):
                payload["metadata"] = {
                    str(key): value
                    for key, value in metadata.items()
                    if isinstance(value, (bool, int, float))
                    or (
                        isinstance(value, str)
                        and len(value) <= 500
                    )
                }
            messages.append(payload)
        total = int(count_row["total"]) if count_row is not None else 0
        return {
            "messages": messages,
            "total": total,
            "offset": normalized_offset,
            "limit": normalized_limit,
            "has_more": normalized_offset + len(messages) < total,
        }

    async def overview(self, *, recent_limit: int = 8) -> dict[str, Any]:
        """Return small operational aggregates for the desktop control surface."""
        return await asyncio.to_thread(self._overview_sync, recent_limit)

    def _overview_sync(self, recent_limit: int) -> dict[str, Any]:
        limit = max(1, min(int(recent_limit), 20))
        with self._connect() as connection:
            message_count = int(
                connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            )
            people_count = int(
                connection.execute("SELECT COUNT(*) FROM people").fetchone()[0]
            )
            event_rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM runtime_events GROUP BY status"
            ).fetchall()
            delivery_rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM deliveries GROUP BY status"
            ).fetchall()
            task_rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM runtime_tasks GROUP BY status"
            ).fetchall()
            recent_rows = connection.execute(
                """
                SELECT sequence, event_id, kind, source, speaker_id, content,
                       event_timestamp, status, error
                FROM runtime_events
                ORDER BY sequence DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        events = {str(row["status"]): int(row["count"]) for row in event_rows}
        deliveries = {
            str(row["status"]): int(row["count"]) for row in delivery_rows
        }
        tasks = {str(row["status"]): int(row["count"]) for row in task_rows}
        return {
            "counts": {
                "messages": message_count,
                "people": people_count,
                "events": events,
                "deliveries": deliveries,
                "tasks": tasks,
            },
            "recent_events": [
                {
                    "sequence": int(row["sequence"]),
                    "event_id": str(row["event_id"]),
                    "kind": str(row["kind"]),
                    "source": str(row["source"]),
                    "speaker_id": str(row["speaker_id"]),
                    "content": str(row["content"])[:500],
                    "timestamp": float(row["event_timestamp"]),
                    "status": str(row["status"]),
                    "error": str(row["error"] or ""),
                }
                for row in recent_rows
            ],
        }

    @staticmethod
    def _stored_event(row: sqlite3.Row | dict[str, Any]) -> StoredEvent:
        result_json = row["result_json"]
        return StoredEvent(
            sequence=int(row["sequence"]),
            event=AgentEvent(
                event_id=str(row["event_id"]),
                kind=str(row["kind"]),
                source=str(row["source"]),
                conversation_id=str(row["conversation_id"]),
                speaker_id=str(row["speaker_id"]),
                audience_ids=tuple(json.loads(row["audience_json"])),
                content=str(row["content"]),
                timestamp=float(row["event_timestamp"]),
                metadata=dict(json.loads(row["metadata_json"])),
            ),
            status=str(row["status"]),
            side_effect_started=bool(row["side_effect_started"]),
            result=dict(json.loads(result_json)) if result_json else None,
            error=str(row["error"] or ""),
        )

    async def add_delivery(self, delivery: Delivery) -> None:
        await asyncio.to_thread(self._add_delivery_sync, delivery)

    def _add_delivery_sync(self, delivery: Delivery) -> None:
        if delivery.status not in _DELIVERY_STATUSES:
            raise ValueError(f"unknown delivery status: {delivery.status}")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO deliveries(
                    delivery_id, event_id, channel, target_json, payload_json,
                    status, attempts, channel_message_id, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(delivery_id) DO NOTHING
                """,
                (
                    delivery.delivery_id,
                    delivery.event_id,
                    delivery.channel,
                    json.dumps(delivery.target, ensure_ascii=False),
                    json.dumps(delivery.payload, ensure_ascii=False),
                    delivery.status,
                    delivery.attempts,
                    delivery.channel_message_id,
                    delivery.error,
                    delivery.created_at,
                    delivery.updated_at,
                ),
            )
            if cursor.rowcount == 0:
                row = connection.execute(
                    "SELECT event_id, channel, target_json, payload_json FROM deliveries WHERE delivery_id=?",
                    (delivery.delivery_id,),
                ).fetchone()
                expected = (
                    delivery.event_id,
                    delivery.channel,
                    delivery.target,
                    delivery.payload,
                )
                actual = (
                    str(row["event_id"]),
                    str(row["channel"]),
                    dict(json.loads(row["target_json"])),
                    dict(json.loads(row["payload_json"])),
                ) if row is not None else ()
                if actual != expected:
                    raise ValueError(
                        f"delivery_id collision with different payload: {delivery.delivery_id}"
                    )
            connection.commit()

    async def list_deliveries(self, *, status: str | None = None) -> list[Delivery]:
        return await asyncio.to_thread(self._list_deliveries_sync, status)

    async def list_dispatchable_deliveries(self) -> list[Delivery]:
        return await asyncio.to_thread(self._list_dispatchable_deliveries_sync)

    def _list_dispatchable_deliveries_sync(self) -> list[Delivery]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT deliveries.*
                FROM deliveries
                LEFT JOIN runtime_events
                  ON runtime_events.event_id = deliveries.event_id
                WHERE deliveries.status=?
                  AND (
                    runtime_events.event_id IS NULL
                    OR runtime_events.status=?
                  )
                ORDER BY deliveries.created_at ASC
                """,
                (DELIVERY_STATUS_PENDING, EVENT_STATUS_COMPLETED),
            ).fetchall()
        return [self._delivery(row) for row in rows]

    def _list_deliveries_sync(self, status: str | None) -> list[Delivery]:
        query = "SELECT * FROM deliveries"
        params: tuple[Any, ...] = ()
        if status:
            if status not in _DELIVERY_STATUSES:
                raise ValueError(f"unknown delivery status: {status}")
            query += " WHERE status=?"
            params = (status,)
        query += " ORDER BY created_at ASC"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._delivery(row) for row in rows]

    async def retry_blocked_delivery(self, delivery_id: str) -> Delivery:
        return await asyncio.to_thread(self._retry_blocked_delivery_sync, delivery_id)

    def _retry_blocked_delivery_sync(self, delivery_id: str) -> Delivery:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE deliveries SET status=?, error='', updated_at=?
                WHERE delivery_id=? AND status=?
                """,
                (DELIVERY_STATUS_PENDING, time.time(), delivery_id, DELIVERY_STATUS_BLOCKED),
            )
            if cursor.rowcount != 1:
                raise ValueError("delivery is not blocked or does not exist")
            row = connection.execute(
                "SELECT * FROM deliveries WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
            connection.commit()
        assert row is not None
        return self._delivery(row)

    async def update_delivery(
        self,
        delivery_id: str,
        *,
        status: str,
        error: str = "",
        channel_message_id: str = "",
        increment_attempts: bool = False,
    ) -> Delivery:
        return await asyncio.to_thread(
            self._update_delivery_sync,
            delivery_id,
            status,
            error,
            channel_message_id,
            increment_attempts,
        )

    def _update_delivery_sync(
        self,
        delivery_id: str,
        status: str,
        error: str,
        channel_message_id: str,
        increment_attempts: bool,
    ) -> Delivery:
        if status not in _DELIVERY_STATUSES:
            raise ValueError(f"unknown delivery status: {status}")
        attempts_sql = "attempts=attempts+1," if increment_attempts else ""
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE deliveries
                SET {attempts_sql} status=?, error=?, channel_message_id=?, updated_at=?
                WHERE delivery_id=?
                """,
                (
                    status,
                    str(error),
                    str(channel_message_id),
                    time.time(),
                    delivery_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown delivery: {delivery_id}")
            row = connection.execute(
                "SELECT * FROM deliveries WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
            connection.commit()
        assert row is not None
        return self._delivery(row)

    @staticmethod
    def _delivery(row: sqlite3.Row) -> Delivery:
        return Delivery(
            delivery_id=str(row["delivery_id"]),
            event_id=str(row["event_id"]),
            channel=str(row["channel"]),
            target=dict(json.loads(row["target_json"])),
            payload=dict(json.loads(row["payload_json"])),
            status=str(row["status"]),
            attempts=int(row["attempts"]),
            channel_message_id=str(row["channel_message_id"]),
            error=str(row["error"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    async def resolve_person(self, channel: str, account_id: str) -> str:
        return await asyncio.to_thread(self._resolve_person_sync, channel, account_id)

    def _resolve_person_sync(self, channel: str, account_id: str) -> str:
        normalized_channel = str(channel or "").strip().lower()
        normalized_account = str(account_id or "").strip()
        if not normalized_channel or not normalized_account:
            raise ValueError("channel and account_id are required")
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT person_id FROM channel_accounts
                WHERE channel=? AND account_id=?
                """,
                (normalized_channel, normalized_account),
            ).fetchone()
            if row is not None:
                person_id = str(row["person_id"])
                connection.execute(
                    "UPDATE people SET last_seen_at=? WHERE person_id=?",
                    (now, person_id),
                )
                connection.execute(
                    """
                    UPDATE channel_accounts SET last_seen_at=?
                    WHERE channel=? AND account_id=?
                    """,
                    (now, normalized_channel, normalized_account),
                )
                connection.commit()
                return person_id

            person_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO people(person_id, display_name, created_at, last_seen_at)
                VALUES (?, '', ?, ?)
                """,
                (person_id, now, now),
            )
            connection.execute(
                """
                INSERT INTO channel_accounts(
                    channel, account_id, person_id, allow_proactive, last_seen_at
                ) VALUES (?, ?, ?, 0, ?)
                """,
                (normalized_channel, normalized_account, person_id, now),
            )
            connection.commit()
            return person_id

    async def link_account(
        self,
        *,
        person_id: str,
        channel: str,
        account_id: str,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._link_account_sync,
            person_id,
            channel,
            account_id,
        )

    def _link_account_sync(
        self,
        person_id: str,
        channel: str,
        account_id: str,
    ) -> dict[str, Any]:
        normalized_person = str(person_id or "").strip()
        normalized_channel = str(channel or "").strip().lower()
        normalized_account = str(account_id or "").strip()
        if not normalized_person or not normalized_channel or not normalized_account:
            raise ValueError("person_id, channel, and account_id are required")
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            person = connection.execute(
                "SELECT person_id FROM people WHERE person_id=?",
                (normalized_person,),
            ).fetchone()
            if person is None:
                connection.rollback()
                raise KeyError(f"unknown person: {normalized_person}")
            connection.execute(
                """
                INSERT INTO channel_accounts(
                    channel, account_id, person_id, allow_proactive, last_seen_at
                ) VALUES (?, ?, ?, 0, ?)
                ON CONFLICT(channel, account_id) DO UPDATE SET
                    person_id=excluded.person_id,
                    last_seen_at=excluded.last_seen_at
                """,
                (normalized_channel, normalized_account, normalized_person, now),
            )
            connection.execute(
                "UPDATE people SET last_seen_at=? WHERE person_id=?",
                (now, normalized_person),
            )
            connection.commit()
        return {
            "person_id": normalized_person,
            "channel": normalized_channel,
            "account_id": normalized_account,
        }

    async def list_people(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_people_sync)

    def _list_people_sync(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            people = connection.execute(
                """
                SELECT person_id, display_name, created_at, last_seen_at
                FROM people ORDER BY created_at ASC
                """
            ).fetchall()
            accounts = connection.execute(
                """
                SELECT channel, account_id, person_id, allow_proactive, last_seen_at
                FROM channel_accounts ORDER BY channel, account_id
                """
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for account in accounts:
            grouped.setdefault(str(account["person_id"]), []).append(
                {
                    "channel": str(account["channel"]),
                    "account_id": str(account["account_id"]),
                    "allow_proactive": bool(account["allow_proactive"]),
                    "last_seen_at": float(account["last_seen_at"]),
                }
            )
        return [
            {
                "person_id": str(person["person_id"]),
                "display_name": str(person["display_name"]),
                "created_at": float(person["created_at"]),
                "last_seen_at": float(person["last_seen_at"]),
                "accounts": grouped.get(str(person["person_id"]), []),
            }
            for person in people
        ]
