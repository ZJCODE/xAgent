from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any

class MemoryStore(ABC):
    """Abstract interface for memory storage operations."""
    
    @abstractmethod
    async def store(self, 
              user_id: str, 
              content: str,
              metadata: Optional[Dict[str, Any]] = None) -> str:
        """Store memory content and return memory ID."""
        pass

    @abstractmethod
    async def retrieve(self, 
                 user_id: str, 
                 query: str,
                 limit: int = 5) -> List[str]:
        """Retrieve relevant memories based on query."""
        pass
