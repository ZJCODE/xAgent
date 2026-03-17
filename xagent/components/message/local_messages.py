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
    DEFAULT_MESSAGE_COUNT = 20
    CONNECT_TIMEOUT = 5.0


class MessageStorageLocal(MessageStorageBase):
    """
    Local persistent message storage backed by SQLite.

    The storage model is append-only per message. Each row stores the user,
    session, message timestamp, and the full serialized Message payload.
    """

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
                for row in conn.execute("PRAGMA table_info(messages)").fetchall()
            }
            expected_columns = {"id", "conversation_id", "timestamp", "message_json"}
            if columns and columns != expected_columns:
                conn.execute("DROP TABLE IF EXISTS messages")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    message_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messages_conversation
                ON messages (conversation_id, id)
                """
            )
            conn.commit()

    async def add_messages(
        self,
        conversation_id: str,
        messages: Union[Message, List[Message]],
        **kwargs,
    ) -> None:
        normalized = messages if isinstance(messages, list) else [messages]
        if not normalized:
            return
        await asyncio.to_thread(self._add_messages_sync, conversation_id, normalized)

    def _add_messages_sync(self, conversation_id: str, messages: List[Message]) -> None:
        rows = [
            (conversation_id, msg.timestamp, msg.model_dump_json())
            for msg in messages
        ]
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO messages (conversation_id, timestamp, message_json)
                VALUES (?, ?, ?)
                """,
                rows,
            )
            conn.commit()

    async def get_messages(
        self,
        conversation_id: str,
        count: int = MessageStorageLocalConfig.DEFAULT_MESSAGE_COUNT,
    ) -> List[Message]:
        if count <= 0:
            raise ValueError("count must be a positive integer")
        return await asyncio.to_thread(self._get_messages_sync, conversation_id, count)

    def _get_messages_sync(self, conversation_id: str, count: int) -> List[Message]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT message_json
                FROM messages
                WHERE conversation_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (conversation_id, count),
            ).fetchall()

        messages: List[Message] = []
        for row in reversed(rows):
            try:
                messages.append(Message.model_validate_json(row["message_json"]))
            except Exception as exc:
                self.logger.warning(
                    "Skipping invalid local message for %s: %s",
                    conversation_id,
                    exc,
                )
        return messages

    async def clear_conversation(self, conversation_id: str) -> None:
        await asyncio.to_thread(self._clear_conversation_sync, conversation_id)

    def _clear_conversation_sync(self, conversation_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            )
            conn.commit()

    async def pop_message(self, conversation_id: str) -> Optional[Message]:
        return await asyncio.to_thread(self._pop_message_sync, conversation_id)

    def _pop_message_sync(self, conversation_id: str) -> Optional[Message]:
        with self._connect() as conn:
            while True:
                row = conn.execute(
                    """
                    SELECT id, message_json
                    FROM messages
                    WHERE conversation_id = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (conversation_id,),
                ).fetchone()
                if row is None:
                    return None

                conn.execute("DELETE FROM messages WHERE id = ?", (row["id"],))
                conn.commit()

                try:
                    message = Message.model_validate_json(row["message_json"])
                except Exception as exc:
                    self.logger.warning(
                        "Skipping invalid popped local message for %s: %s",
                        conversation_id,
                        exc,
                    )
                    continue

                if not self._is_tool_message(message):
                    return message

    def _is_tool_message(self, message: Message) -> bool:
        return message.type in {MessageType.FUNCTION_CALL, MessageType.FUNCTION_CALL_OUTPUT}

    async def get_message_count(self, conversation_id: str) -> int:
        return await asyncio.to_thread(self._get_message_count_sync, conversation_id)

    def _get_message_count_sync(self, conversation_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS message_count
                FROM messages
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()
        return int(row["message_count"]) if row is not None else 0

    def get_conversation_info(self, conversation_id: str) -> Dict[str, str]:
        return {
            "conversation_id": conversation_id,
            "backend": "local",
            "conversation_key": conversation_id,
            "path": str(self.path),
        }

    def __repr__(self) -> str:
        return f"MessageStorageLocal(path='{self.path}')"
