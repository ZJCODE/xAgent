from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ..message.base_messages import MessageStorageBase as _MsgStorage


class MemoryStorageBase(ABC):
    """Abstract interface for memory storage operations."""

    @abstractmethod
    async def add(self,
                  user_id: str,
                  session_id: str,
                  messages: List[Dict[str, Any]]
                  ):
        """
        Add messages and conditionally trigger long-term memory extraction.

        The memory system reads conversation history directly from the
        associated *message_storage* when extraction is triggered, so
        ``messages`` is only used for counting and keyword detection.
        """
        pass

    @abstractmethod
    async def store(self, 
              user_id: str, 
              content: str) -> Optional[str]:
        """Store memory content and return memory ID."""
        pass

    @abstractmethod
    async def retrieve(self, 
                 user_id: str, 
                 query: str,
                 limit: int = 5,
                 query_context: Optional[str] = None,
                 enable_query_process: bool = False
                 ) -> Optional[List[str]]:
        """Retrieve relevant memories based on query."""
        pass

    @abstractmethod
    async def extract_meta(self, 
                      user_id: str, 
                      days: int = 1) -> Optional[List[str]]:
        """Extract metadata from memories within a time frame."""
        pass

    @abstractmethod
    async def clear(self, user_id: str) -> None:
        """Clear all memories for a user."""
        pass

    @abstractmethod
    async def delete(self, memory_ids: List[str]):
        """Delete memories by their IDs."""
        pass