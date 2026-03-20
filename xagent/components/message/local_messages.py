import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Union

from .base_messages import MessageStorageBase
from ...schemas import Message, MessageType


class MessageStorageLocalConfig:
    """Configuration constants for MessageStorageLocal."""

    DEFAULT_PATH = "~/.xagent/messages.sqlite3"
    DEFAULT_MESSAGE_COUNT = 100
    CONNECT_TIMEOUT = 5.0
    TABLE_NAME = "messages"
    LEGACY_COLUMNS = {"id", "conversation_id", "timestamp", "message_json"}
    CURRENT_COLUMNS = {"id", "timestamp", "message_json"}


class MessageStorageLocal(MessageStorageBase):
    """Local persistent single-stream message storage backed by SQLite."""

    def __init__(self, path: Optional[str] = None):
        self.path = Path(path or MessageStorageLocalConfig.DEFAULT_PATH).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        self._initialize_database()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=MessageStorageLocalConfig.CONNECT_TIMEOUT)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize_database(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            columns = {
                row["name"]
                for row in conn.execute(
                    f"PRAGMA table_info({MessageStorageLocalConfig.TABLE_NAME})"
                ).fetchall()
            }

            if not columns:
                self._create_current_schema(conn)
            elif columns == MessageStorageLocalConfig.CURRENT_COLUMNS:
                self._ensure_current_indexes(conn)
            elif columns == MessageStorageLocalConfig.LEGACY_COLUMNS:
                self._migrate_legacy_schema(conn)
            else:
                self.logger.warning(
                    "Unexpected messages schema at %s; recreating storage table.",
                    self.path,
                )
                conn.execute(f"DROP TABLE IF EXISTS {MessageStorageLocalConfig.TABLE_NAME}")
                self._create_current_schema(conn)

            conn.commit()

    def _create_current_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {MessageStorageLocalConfig.TABLE_NAME} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                message_json TEXT NOT NULL
            )
            """
        )
        self._ensure_current_indexes(conn)

    def _ensure_current_indexes(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{MessageStorageLocalConfig.TABLE_NAME}_id
            ON {MessageStorageLocalConfig.TABLE_NAME} (id)
            """
        )

    def _migrate_legacy_schema(self, conn: sqlite3.Connection) -> None:
        legacy_table = f"{MessageStorageLocalConfig.TABLE_NAME}_legacy"
        conn.execute(f"ALTER TABLE {MessageStorageLocalConfig.TABLE_NAME} RENAME TO {legacy_table}")
        self._create_current_schema(conn)
        conn.execute(
            f"""
            INSERT INTO {MessageStorageLocalConfig.TABLE_NAME} (id, timestamp, message_json)
            SELECT id, timestamp, message_json
            FROM {legacy_table}
            ORDER BY id
            """
        )
        conn.execute(f"DROP TABLE {legacy_table}")
        self.logger.info("Migrated legacy conversation-scoped message storage at %s", self.path)

    async def add_messages(
        self,
        messages: Union[Message, List[Message]],
        **kwargs,
    ) -> None:
        normalized = messages if isinstance(messages, list) else [messages]
        if not normalized:
            return
        await asyncio.to_thread(self._add_messages_sync, normalized)

    def _add_messages_sync(self, messages: List[Message]) -> None:
        rows = [(msg.timestamp, msg.model_dump_json()) for msg in messages]
        with self._connect() as conn:
            conn.executemany(
                f"""
                INSERT INTO {MessageStorageLocalConfig.TABLE_NAME} (timestamp, message_json)
                VALUES (?, ?)
                """,
                rows,
            )
            conn.commit()

    async def get_messages(
        self,
        count: int = MessageStorageLocalConfig.DEFAULT_MESSAGE_COUNT,
        offset: int = 0,
    ) -> List[Message]:
        if count <= 0:
            raise ValueError("count must be a positive integer")
        return await asyncio.to_thread(self._get_messages_sync, count, offset)

    def _get_messages_sync(self, count: int, offset: int = 0) -> List[Message]:
        with self._connect() as conn:
            rows = conn.execute(
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
            except Exception as exc:
                self.logger.warning("Skipping invalid local message: %s", exc)
        return messages

    async def clear_messages(self) -> None:
        await asyncio.to_thread(self._clear_messages_sync)

    def _clear_messages_sync(self) -> None:
        with self._connect() as conn:
            conn.execute(f"DELETE FROM {MessageStorageLocalConfig.TABLE_NAME}")
            conn.commit()

    async def pop_message(self) -> Optional[Message]:
        return await asyncio.to_thread(self._pop_message_sync)

    def _pop_message_sync(self) -> Optional[Message]:
        with self._connect() as conn:
            while True:
                row = conn.execute(
                    f"""
                    SELECT id, message_json
                    FROM {MessageStorageLocalConfig.TABLE_NAME}
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    return None

                conn.execute(
                    f"DELETE FROM {MessageStorageLocalConfig.TABLE_NAME} WHERE id = ?",
                    (row["id"],),
                )
                conn.commit()

                try:
                    message = Message.model_validate_json(row["message_json"])
                except Exception as exc:
                    self.logger.warning("Skipping invalid popped local message: %s", exc)
                    continue

                if not self._is_tool_message(message):
                    return message

    def _is_tool_message(self, message: Message) -> bool:
        return message.type in {MessageType.FUNCTION_CALL, MessageType.FUNCTION_CALL_OUTPUT}

    async def get_message_count(self) -> int:
        return await asyncio.to_thread(self._get_message_count_sync)

    def _get_message_count_sync(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
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
