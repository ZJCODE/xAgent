"""SQLite-backed message-history store."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...domain.message_records import MessageBatch, StoredMessage
from ...domain import Message


class SQLiteMessageStore:
    """Persistent ordered message store for one agent stream."""

    DEFAULT_MESSAGE_COUNT = 100
    CONNECT_TIMEOUT = 5.0
    TABLE_NAME = "messages"
    CURRENT_COLUMNS = {"id", "timestamp", "message_json"}

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger(self.__class__.__name__)
        self._initialize_database()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=self.CONNECT_TIMEOUT)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            columns = {
                row["name"]
                for row in connection.execute(f"PRAGMA table_info({self.TABLE_NAME})").fetchall()
            }

            if not columns:
                self._create_current_schema(connection)
            elif columns == self.CURRENT_COLUMNS:
                self._ensure_current_indexes(connection)
            else:
                raise RuntimeError(
                    f"Unexpected messages schema at {self.path}. "
                    "Delete the development database or migrate it explicitly."
                )

            connection.commit()

    def _create_current_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                message_json TEXT NOT NULL
            )
            """
        )
        self._ensure_current_indexes(connection)

    def _ensure_current_indexes(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{self.TABLE_NAME}_id
            ON {self.TABLE_NAME} (id)
            """
        )

    async def add_messages(self, messages: MessageBatch, **kwargs: Any) -> None:
        normalized_messages = self.normalize_messages(messages)
        if not normalized_messages:
            return
        await asyncio.to_thread(self._add_messages_sync, normalized_messages)

    def _add_messages_sync(self, messages: List[Message]) -> None:
        rows = [(message.timestamp, message.model_dump_json()) for message in messages]
        with self._connect() as connection:
            connection.executemany(
                f"""
                INSERT INTO {self.TABLE_NAME} (timestamp, message_json)
                VALUES (?, ?)
                """,
                rows,
            )
            connection.commit()

    async def get_messages(
        self,
        count: int = DEFAULT_MESSAGE_COUNT,
        offset: int = 0,
    ) -> List[Message]:
        stored_messages = await self.get_stored_messages(count=count, offset=offset)
        return [stored.message for stored in stored_messages]

    async def get_stored_messages(
        self,
        count: int = DEFAULT_MESSAGE_COUNT,
        offset: int = 0,
    ) -> List[StoredMessage]:
        count, offset = self.validate_pagination(count, offset)
        return await asyncio.to_thread(self._get_stored_messages_sync, count, offset)

    def _get_stored_messages_sync(self, count: int, offset: int = 0) -> List[StoredMessage]:
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, message_json
                FROM {self.TABLE_NAME}
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (count, offset),
            ).fetchall()

        return self._stored_messages_from_rows(reversed(rows))

    async def clear_messages(self) -> None:
        await asyncio.to_thread(self._clear_messages_sync)

    def _clear_messages_sync(self) -> None:
        with self._connect() as connection:
            connection.execute(f"DELETE FROM {self.TABLE_NAME}")
            connection.commit()

    async def pop_message(self) -> Optional[Message]:
        return await asyncio.to_thread(self._pop_message_sync)

    def _pop_message_sync(self) -> Optional[Message]:
        with self._connect() as connection:
            while True:
                row = connection.execute(
                    f"""
                    SELECT id, message_json
                    FROM {self.TABLE_NAME}
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    return None

                connection.execute(f"DELETE FROM {self.TABLE_NAME} WHERE id = ?", (row["id"],))
                connection.commit()

                try:
                    return Message.model_validate_json(row["message_json"])
                except Exception as exception:
                    self.logger.warning("Skipping invalid popped message: %s", exception)

    async def get_message_count(self) -> int:
        return await asyncio.to_thread(self._get_message_count_sync)

    def _get_message_count_sync(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS message_count
                FROM {self.TABLE_NAME}
                """
            ).fetchone()
        return int(row["message_count"]) if row is not None else 0

    async def get_latest_message_id(self) -> int:
        return await asyncio.to_thread(self._get_latest_message_id_sync)

    def _get_latest_message_id_sync(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT COALESCE(MAX(id), 0) AS latest_id
                FROM {self.TABLE_NAME}
                """
            ).fetchone()
        return int(row["latest_id"]) if row is not None else 0

    async def get_messages_by_id_range(
        self,
        start_exclusive: int = 0,
        end_inclusive: Optional[int] = None,
    ) -> List[StoredMessage]:
        normalized_start = self._non_negative_int(start_exclusive)
        normalized_end = (
            await self.get_latest_message_id()
            if end_inclusive is None
            else self._non_negative_int(end_inclusive)
        )

        if normalized_end <= normalized_start:
            return []

        return await asyncio.to_thread(
            self._get_messages_by_id_range_sync,
            normalized_start,
            normalized_end,
        )

    def _get_messages_by_id_range_sync(
        self,
        start_exclusive: int,
        end_inclusive: int,
    ) -> List[StoredMessage]:
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, message_json
                FROM {self.TABLE_NAME}
                WHERE id > ? AND id <= ?
                ORDER BY id ASC
                """,
                (start_exclusive, end_inclusive),
            ).fetchall()

        return self._stored_messages_from_rows(rows)

    async def search_messages(
        self,
        query: str,
        date_start: Optional[str] = None,
        date_end: Optional[str] = None,
        max_results: int = 500,
    ) -> str:
        if not query:
            return ""
        return await asyncio.to_thread(
            self._search_messages_sync,
            query,
            date_start,
            date_end,
            max_results,
        )

    def _search_messages_sync(
        self,
        query: str,
        date_start: Optional[str],
        date_end: Optional[str],
        max_results: int,
    ) -> str:
        conditions = [f"json_extract(message_json, '$.content') LIKE ? ESCAPE '\\'"]
        params: list[Any] = [f"%{self._escape_like(query)}%"]

        if date_start:
            start_ts = self._date_str_to_timestamp(date_start)
            if start_ts is not None:
                conditions.append("json_extract(message_json, '$.timestamp') >= ?")
                params.append(start_ts)

        if date_end:
            end_ts = self._date_str_to_timestamp(date_end, is_end=True)
            if end_ts is not None:
                conditions.append("json_extract(message_json, '$.timestamp') <= ?")
                params.append(end_ts)

        where = " AND ".join(conditions)

        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, message_json
                FROM {self.TABLE_NAME}
                WHERE {where}
                ORDER BY id DESC
                LIMIT ?
                """,
                (*params, max_results),
            ).fetchall()

        matched = [
            self._format_search_match(stored.message)
            for stored in reversed(self._stored_messages_from_rows(rows))
        ]
        return "\n---\n".join(matched)

    def get_stream_info(self) -> Dict[str, str]:
        return {
            "stream": "sqlite",
            "backend": "sqlite",
            "path": str(self.path),
        }

    @staticmethod
    def normalize_messages(messages: MessageBatch) -> List[Message]:
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

    def _stored_messages_from_rows(self, rows) -> List[StoredMessage]:
        messages: List[StoredMessage] = []
        for row in rows:
            try:
                messages.append(
                    StoredMessage(
                        id=int(row["id"]),
                        message=Message.model_validate_json(row["message_json"]),
                    )
                )
            except Exception as exception:
                self.logger.warning("Skipping invalid message row: %s", exception)
        return messages

    @staticmethod
    def _format_search_match(message: Message) -> str:
        ts = datetime.fromtimestamp(message.timestamp).strftime("%Y-%m-%d %H:%M:%S")
        sender = message.sender_id or message.role.value
        return f"[{ts}][speaker={sender}]\n{message.content.strip()}"

    @staticmethod
    def _date_str_to_timestamp(date_str: str, is_end: bool = False) -> Optional[float]:
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            if is_end:
                dt = dt.replace(hour=23, minute=59, second=59)
            return dt.replace(tzinfo=timezone.utc).timestamp()
        except (ValueError, OverflowError):
            return None

    @staticmethod
    def _escape_like(query: str) -> str:
        for char in ("%", "_"):
            query = query.replace(char, f"\\{char}")
        return query

    @staticmethod
    def _non_negative_int(value: object) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    def __repr__(self) -> str:
        return f"SQLiteMessageStore(path='{self.path}')"
