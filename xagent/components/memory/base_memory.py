from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any

class MemoryStore(ABC):
    """Abstract interface for memory storage operations."""
    
    @abstractmethod
    def store(self, 
              user_id: str, 
              content: str,
              metadata: Optional[Dict[str, Any]] = None) -> str:
        """Store memory content and return memory ID."""
        pass

    @abstractmethod
    def retrieve(self, 
                 user_id: str, 
                 query: str,
                 limit: int = 5) -> List[Dict[str, Any]]:
        """Retrieve relevant memories based on query."""
        pass

    @abstractmethod
    def get(self, 
            user_id: str, 
            memory_id: str) -> Optional[Dict[str, Any]]:
        """Get specific memory by ID."""
        pass

    @abstractmethod
    def update(self, 
               user_id: str, 
               memory_id: str, 
               content: str,
               metadata: Optional[Dict[str, Any]] = None) -> bool:
        """Update memory content and return success status."""
        pass

    @abstractmethod
    def delete(self, 
               user_id: str, 
               memory_id: str) -> bool:
        """Delete memory and return success status."""
        pass

    @abstractmethod
    def list_memories(self, 
                      user_id: str,
                      limit: int = 50,
                      offset: int = 0) -> List[Dict[str, Any]]:
        """List user's memories with pagination."""
        pass