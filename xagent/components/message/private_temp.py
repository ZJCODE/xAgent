"""Temporary message storage for private conversations."""

from __future__ import annotations

from typing import Dict, List, Optional

from .base import MessageBatch, MessageStorageBase
from ...schemas import Message


class MessageStoragePrivateTemp(MessageStorageBase):
    """Message storage for private mode.

    Private conversations need a short-lived history buffer that disappears as
    soon as the agent returns to normal mode. This backend keeps that policy
    explicit instead of exposing the incidental in-memory implementation detail.
    """

    def __init__(self, initial_messages: MessageBatch | None = None) -> None:
        self._messages: List[Message] = []
        if initial_messages is not None:
            self._messages.extend(self.normalize_messages(initial_messages))

    async def add_messages(
        self,
        messages: MessageBatch,
        **kwargs,
    ) -> None:
        self._messages.extend(self.normalize_messages(messages))

    async def get_messages(
        self,
        count: int = 20,
        offset: int = 0,
    ) -> List[Message]:
        count, offset = self.validate_pagination(count, offset)
        end_index = len(self._messages) - offset
        if end_index <= 0:
            return []

        start_index = max(0, end_index - count)
        return list(self._messages[start_index:end_index])

    async def clear_messages(self) -> None:
        self._messages.clear()

    async def pop_message(self) -> Optional[Message]:
        if not self._messages:
            return None
        return self._messages.pop()

    async def get_message_count(self) -> int:
        return len(self._messages)

    def get_stream_info(self) -> Dict[str, str]:
        return {
            "stream": "private_temp",
            "backend": "private_temp",
            "message_count": str(len(self._messages)),
        }
