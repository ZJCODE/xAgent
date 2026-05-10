"""SQLite-backed message-history storage."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

from .base import MessageBatch, MessageStorageBase
from ...schemas import Message, MessageType


class MessageStorageLocalConfig:
    """Configuration constants for ``MessageStorageLocal``."""

    DEFAULT_PATH = "~/.xagent/messages/messages.sqlite3"
    DEFAULT_MESSAGE_COUNT = 100
    CONNECT_TIMEOUT = 5.0
    TABLE_NAME = "messages"
    CURRENT_COLUMNS = {"id", "timestamp", "message_json"}


class MessageStorageLocal(MessageStorageBase):
    """Persistent message storage for one ordered stream, backed by SQLite."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = Path(path or MessageStorageLocalConfig.DEFAULT_PATH).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger(self.__class__.__name__)
        self._initialize_database()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path),
            timeout=MessageStorageLocalConfig.CONNECT_TIMEOUT,
        )
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            columns = {
                row["name"]
                for row in connection.execute(
                    f"PRAGMA table_info({MessageStorageLocalConfig.TABLE_NAME})"
                ).fetchall()
            }

            if not columns:
                self._create_current_schema(connection)
            elif columns == MessageStorageLocalConfig.CURRENT_COLUMNS:
                self._ensure_current_indexes(connection)
            else:
                self.logger.warning(
                    "Unexpected messages schema at %s; recreating storage table.",
                    self.path,
                )
                connection.execute(f"DROP TABLE IF EXISTS {MessageStorageLocalConfig.TABLE_NAME}")
                self._create_current_schema(connection)

            connection.commit()

    def _create_current_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {MessageStorageLocalConfig.TABLE_NAME} (
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
            CREATE INDEX IF NOT EXISTS idx_{MessageStorageLocalConfig.TABLE_NAME}_id
            ON {MessageStorageLocalConfig.TABLE_NAME} (id)
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
                INSERT INTO {MessageStorageLocalConfig.TABLE_NAME} (timestamp, message_json)
                VALUES (?, ?)
                """,
                rows,
            )
            connection.commit()

    async def get_messages(
        self,
        count: int = MessageStorageLocalConfig.DEFAULT_MESSAGE_COUNT,
        offset: int = 0,
    ) -> List[Message]:
        count, offset = self.validate_pagination(count, offset)
        return await asyncio.to_thread(self._get_messages_sync, count, offset)

    def _get_messages_sync(self, count: int, offset: int = 0) -> List[Message]:
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT message_json
                FROM {MessageStorageLocalConfig.TABLE_NAME}
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
            connection.execute(f"DELETE FROM {MessageStorageLocalConfig.TABLE_NAME}")
            connection.commit()

    async def pop_message(self) -> Optional[Message]:
        return await asyncio.to_thread(self._pop_message_sync)

    def _pop_message_sync(self) -> Optional[Message]:
        with self._connect() as connection:
            while True:
                row = connection.execute(
                    f"""
                    SELECT id, message_json
                    FROM {MessageStorageLocalConfig.TABLE_NAME}
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    return None

                connection.execute(
                    f"DELETE FROM {MessageStorageLocalConfig.TABLE_NAME} WHERE id = ?",
                    (row["id"],),
                )
                connection.commit()

                try:
                    message = Message.model_validate_json(row["message_json"])
                except Exception as exception:
                    self.logger.warning("Skipping invalid popped local message: %s", exception)
                    continue

                if not self._is_tool_message(message):
                    return message

    def _is_tool_message(self, message: Message) -> bool:
        return message.type in {MessageType.FUNCTION_CALL, MessageType.FUNCTION_CALL_OUTPUT}

    async def get_message_count(self) -> int:
        return await asyncio.to_thread(self._get_message_count_sync)

    def _get_message_count_sync(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS message_count
                FROM {MessageStorageLocalConfig.TABLE_NAME}
                """
            ).fetchone()
        return int(row["message_count"]) if row is not None else 0

    def get_stream_info(self) -> Dict[str, str]:
        return {
            "stream": "local",
            "backend": "local",
            "path": str(self.path),
        }

    def __repr__(self) -> str:
        return f"MessageStorageLocal(path='{self.path}')"
