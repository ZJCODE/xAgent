from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
from pydantic import BaseModel


class MessageBufferBase(ABC):
    """Abstract interface for message buffering operations."""

    @abstractmethod
    async def add_messages(self, user_id: str, messages: List[Dict[str, Any]]) -> None:
        """
        Add messages to the buffer for a specific user.
        """
        pass

    @abstractmethod
    async def get_messages(self, user_id: str) -> List[Dict[str, Any]]:
        """
        Get messages from the buffer for a specific user.
        """
        pass


    @abstractmethod
    async def get_message_count(self, user_id: str) -> int:
        """
        Get the count of messages in the buffer for a specific user.
        """
        pass

    @abstractmethod
    async def keep_recent_messages(self, user_id: str, keep_count: int) -> None:
        """
        Keep only the most recent messages in the buffer for a specific user.
        """
        pass


    @abstractmethod
    async def clear_messages(self, user_id: str) -> None:
        """
        Clear all messages from the buffer for a specific user.
        """
        pass