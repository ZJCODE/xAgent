from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any

class MemoryStore(ABC):
    """Abstract interface for memory storage operations."""
    
    @abstractmethod
    async def store(self, 
              user_id: str, 
              content: str,
              metadata: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """Store memory content and return memory ID."""
        pass

    @abstractmethod
    async def retrieve(self, 
                 user_id: str, 
                 query: str,
                 limit: int = 5) -> Optional[List[str]]:
        """Retrieve relevant memories based on query."""
        pass

    @abstractmethod
    async def extract_meta(self, 
                      user_id: str, 
                      days: int = 1) -> Optional[List[str]]:
        """Extract metadata from memories within a time frame."""
        pass