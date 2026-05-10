"""Message storage interfaces.

Message storage is the short-term conversation history layer. It persists
``Message`` objects in one ordered stream and deliberately knows nothing about
long-term diary memory or summary generation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Dict, List, Optional, Union

from ...schemas import Message

MessageBatch = Union[Message, Sequence[Message]]


class MessageStorageBase(ABC):
    """Interface for message-history storage backends."""

    @abstractmethod
    async def add_messages(
        self,
        messages: MessageBatch,
        **kwargs,
    ) -> None:
        """Append one or more messages to the stream."""
        raise NotImplementedError

    @abstractmethod
    async def get_messages(
        self,
        count: int = 20,
        offset: int = 0,
    ) -> List[Message]:
        """Return recent messages ordered from oldest to newest.

        Args:
            count: Number of messages to retrieve. Must be positive.
            offset: Number of recent messages to skip. Must be non-negative.
        """
        raise NotImplementedError

    @abstractmethod
    async def clear_messages(self) -> None:
        """Remove all messages from the stream."""
        raise NotImplementedError

    @abstractmethod
    async def pop_message(self) -> Optional[Message]:
        """Remove and return the latest non-skipped message, if any."""
        raise NotImplementedError

    async def get_message_count(self) -> int:
        """Return the total number of stored messages."""
        try:
            messages = await self.get_messages(999999)
        except Exception:
            return 0
        return len(messages)

    async def has_messages(self) -> bool:
        """Return whether the stream contains at least one message."""
        return await self.get_message_count() > 0

    def get_stream_info(self) -> Dict[str, str]:
        """Return backend metadata suitable for diagnostics."""
        return {
            "stream": "default",
            "backend": self.__class__.__name__.lower(),
        }

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

    def __str__(self) -> str:
        return f"{self.__class__.__name__}()"

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}()"
