"""SQLite-backed message-history storage."""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Union

from ...schemas import Message


MessageBatch = Union[Message, Sequence[Message]]


class MessageStorageConfig:
    """Configuration constants for ``MessageStorage``."""

    DEFAULT_MESSAGE_COUNT = 100
    CONNECT_TIMEOUT = 5.0
    TABLE_NAME = "messages"
    CURRENT_COLUMNS = {"id", "timestamp", "message_json"}


class MessageStorage:
    """Persistent message storage for one ordered stream, backed by SQLite."""

    def __init__(self, path: str) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger(self.__class__.__name__)
        self._initialize_database()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path),
            timeout=MessageStorageConfig.CONNECT_TIMEOUT,
        )
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            columns = {
                row["name"]
                for row in connection.execute(
                    f"PRAGMA table_info({MessageStorageConfig.TABLE_NAME})"
                ).fetchall()
            }

            if not columns:
                self._create_current_schema(connection)
            elif columns == MessageStorageConfig.CURRENT_COLUMNS:
                self._ensure_current_indexes(connection)
            else:
                self.logger.warning(
                    "Unexpected messages schema at %s; recreating storage table.",
                    self.path,
                )
                connection.execute(f"DROP TABLE IF EXISTS {MessageStorageConfig.TABLE_NAME}")
                self._create_current_schema(connection)

            connection.commit()

    def _create_current_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {MessageStorageConfig.TABLE_NAME} (
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
            CREATE INDEX IF NOT EXISTS idx_{MessageStorageConfig.TABLE_NAME}_id
            ON {MessageStorageConfig.TABLE_NAME} (id)
            """
        )

    async def add_messages(
        self,
        messages: MessageBatch,
        **kwargs,
    ) -> None:
        normalized_messages = self.normalize_messages(messages)
        if not normalized_messages:
            return
        await asyncio.to_thread(self._add_messages_sync, normalized_messages)

    def _add_messages_sync(self, messages: List[Message]) -> None:
        rows = [(message.timestamp, message.model_dump_json()) for message in messages]
        with self._connect() as connection:
            connection.executemany(
                f"""
                INSERT INTO {MessageStorageConfig.TABLE_NAME} (timestamp, message_json)
                VALUES (?, ?)
                """,
                rows,
            )
            connection.commit()

    async def get_messages(
        self,
        count: int = MessageStorageConfig.DEFAULT_MESSAGE_COUNT,
        offset: int = 0,
    ) -> List[Message]:
        count, offset = self.validate_pagination(count, offset)
        return await asyncio.to_thread(self._get_messages_sync, count, offset)

    def _get_messages_sync(self, count: int, offset: int = 0) -> List[Message]:
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT message_json
                FROM {MessageStorageConfig.TABLE_NAME}
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (count, offset),
            ).fetchall()

        messages: List[Message] = []
        for row in reversed(rows):
            try:
                messages.append(Message.model_validate_json(row["message_json"]))
            except Exception as exception:
                self.logger.warning("Skipping invalid local message: %s", exception)
        return messages

    async def clear_messages(self) -> None:
        await asyncio.to_thread(self._clear_messages_sync)

    def _clear_messages_sync(self) -> None:
        with self._connect() as connection:
            connection.execute(f"DELETE FROM {MessageStorageConfig.TABLE_NAME}")
            connection.commit()

    async def pop_message(self) -> Optional[Message]:
        return await asyncio.to_thread(self._pop_message_sync)

    def _pop_message_sync(self) -> Optional[Message]:
        with self._connect() as connection:
            while True:
                row = connection.execute(
                    f"""
                    SELECT id, message_json
                    FROM {MessageStorageConfig.TABLE_NAME}
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    return None

                connection.execute(
                    f"DELETE FROM {MessageStorageConfig.TABLE_NAME} WHERE id = ?",
                    (row["id"],),
                )
                connection.commit()

                try:
                    message = Message.model_validate_json(row["message_json"])
                except Exception as exception:
                    self.logger.warning("Skipping invalid popped local message: %s", exception)
                    continue

                return message

    async def get_message_count(self) -> int:
        return await asyncio.to_thread(self._get_message_count_sync)

    def _get_message_count_sync(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS message_count
                FROM {MessageStorageConfig.TABLE_NAME}
                """
            ).fetchone()
        return int(row["message_count"]) if row is not None else 0

    async def get_latest_message_cursor(self) -> int:
        return await asyncio.to_thread(self._get_latest_message_cursor_sync)

    def _get_latest_message_cursor_sync(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT COALESCE(MAX(id), 0) AS latest_id
                FROM {MessageStorageConfig.TABLE_NAME}
                """
            ).fetchone()
        return int(row["latest_id"]) if row is not None else 0

    async def get_messages_in_cursor_range(
        self,
        start_exclusive: int = 0,
        end_inclusive: Optional[int] = None,
    ) -> List[Message]:
        try:
            normalized_start = max(0, int(start_exclusive))
        except (TypeError, ValueError):
            normalized_start = 0

        if end_inclusive is None:
            normalized_end = await self.get_latest_message_cursor()
        else:
            try:
                normalized_end = max(0, int(end_inclusive))
            except (TypeError, ValueError):
                normalized_end = 0

        if normalized_end <= normalized_start:
            return []

        return await asyncio.to_thread(
            self._get_messages_in_cursor_range_sync,
            normalized_start,
            normalized_end,
        )

    def _get_messages_in_cursor_range_sync(
        self,
        start_exclusive: int,
        end_inclusive: int,
    ) -> List[Message]:
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT message_json
                FROM {MessageStorageConfig.TABLE_NAME}
                WHERE id > ? AND id <= ?
                ORDER BY id ASC
                """,
                (start_exclusive, end_inclusive),
            ).fetchall()

        messages: List[Message] = []
        for row in rows:
            try:
                messages.append(Message.model_validate_json(row["message_json"]))
            except Exception as exception:
                self.logger.warning("Skipping invalid local message: %s", exception)
        return messages

    async def cursor_for_message_count(self, message_count: int) -> int:
        try:
            normalized_count = max(0, int(message_count))
        except (TypeError, ValueError):
            normalized_count = 0

        if normalized_count <= 0:
            return 0
        return await asyncio.to_thread(self._cursor_for_message_count_sync, normalized_count)

    def _cursor_for_message_count_sync(self, message_count: int) -> int:
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT id
                FROM {MessageStorageConfig.TABLE_NAME}
                ORDER BY id ASC
                LIMIT 1 OFFSET ?
                """,
                (message_count - 1,),
            ).fetchone()
        return int(row["id"]) if row is not None else 0

    async def search_messages(
        self,
        query: str,
        date_start: Optional[str] = None,
        date_end: Optional[str] = None,
        max_results: int = 500,
    ) -> str:
        """Search messages by keyword using SQLite json_extract + LIKE."""
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
        from datetime import datetime, timezone

        conditions = [
            f"json_extract(message_json, '$.content') LIKE ? ESCAPE '\\'"
        ]
        params: list = [f"%{self._escape_like(query)}%"]  # noqa: RUF015

        if date_start:
            try:
                start_dt = datetime.strptime(date_start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                conditions.append("json_extract(message_json, '$.timestamp') >= ?")
                params.append(start_dt.timestamp())
            except (ValueError, OverflowError):
                pass

        if date_end:
            try:
                end_dt = datetime.strptime(date_end, "%Y-%m-%d").replace(
                    hour=23, minute=59, second=59, tzinfo=timezone.utc
                )
                conditions.append("json_extract(message_json, '$.timestamp') <= ?")
                params.append(end_dt.timestamp())
            except (ValueError, OverflowError):
                pass

        where = " AND ".join(conditions)
        table = MessageStorageConfig.TABLE_NAME

        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, message_json
                FROM {table}
                WHERE {where}
                ORDER BY id DESC
                LIMIT ?
                """,
                (*params, max_results),
            ).fetchall()

        if not rows:
            return ""

        matched: list[str] = []
        for row in reversed(rows):
            try:
                msg = Message.model_validate_json(row["message_json"])
            except Exception:
                continue
            matched.append(self._format_search_match(msg))

        return "\n---\n".join(matched)

    @staticmethod
    def _escape_like(query: str) -> str:
        """Escape special LIKE wildcard characters in user query."""
        for char in ("%", "_"):
            query = query.replace(char, f"\\{char}")
        return query

    def get_stream_info(self) -> Dict[str, str]:
        return {
            "stream": "local",
            "backend": "local",
            "path": str(self.path),
        }

    def __repr__(self) -> str:
        return f"MessageStorage(path='{self.path}')"

    def __str__(self) -> str:
        return f"MessageStorage(path='{self.path}')"

    async def has_messages(self) -> bool:
        """Return whether the stream contains at least one message."""
        return await self.get_message_count() > 0

    @staticmethod
    def normalize_messages(messages: MessageBatch) -> List[Message]:
        """Normalize caller input to a concrete list of ``Message`` objects."""
        if isinstance(messages, Message):
            return [messages]
        normalized = list(messages)
        if not all(isinstance(message, Message) for message in normalized):
            raise TypeError("messages must be a Message or a sequence of Message instances")
        return normalized

    @staticmethod
    def validate_pagination(count: int, offset: int = 0) -> tuple[int, int]:
        """Validate and normalize message pagination arguments."""
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

    @staticmethod
    def _format_search_match(message: Message) -> str:
        ts = datetime.fromtimestamp(message.timestamp).strftime("%Y-%m-%d %H:%M:%S")
        sender = message.sender_id or message.role.value
        header = f"[{ts}][speaker={sender}]"
        if message.channel:
            header += f"[channel={message.channel}]"
        if message.room_name:
            safe_room = message.room_name.replace("\n", " ").replace("]", "")
            header += f"[room={safe_room}]"
        return f"{header}\n{message.content.strip()}"

    @staticmethod
    def _date_str_to_timestamp(date_str: str, is_end: bool = False) -> float:
        """Convert YYYY-MM-DD to a UTC timestamp."""
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            if is_end:
                dt = dt.replace(hour=23, minute=59, second=59)
            return dt.replace(tzinfo=timezone.utc).timestamp()
        except (ValueError, OverflowError):
            return 0.0
