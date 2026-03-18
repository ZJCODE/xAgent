from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ..message.base_messages import MessageStorageBase as _MsgStorage


class MemoryStorageBase(ABC):
    """Abstract interface for memory storage operations."""

    @abstractmethod
    async def add(self,
                  memory_key: str,
                  messages: List[Dict[str, Any]]
                  ):
        """
        Add messages and conditionally trigger long-term memory extraction.

        The memory system reads message history directly from the
        associated *message_storage* when extraction is triggered, so
        ``messages`` is only used for unread-activity counting and explicit
        remember-this detection.
        """
        pass

    @abstractmethod
    async def store(self, 
              memory_key: str,
              content: str) -> Optional[str]:
        """Store memory content and return memory ID."""
        pass

    @abstractmethod
    async def retrieve(self, 
                 memory_key: str,
                 query: str,
                 limit: int = 5,
                 ) -> Optional[List[Dict[str, Any]]]:
        """Retrieve relevant memories based on query."""
        pass

    @abstractmethod
    async def clear(self, memory_key: str) -> None:
        """Clear all memories for the given memory key."""
        pass

    @abstractmethod
    async def delete(self, memory_ids: List[str]):
        """Delete memories by their IDs."""
        pass
