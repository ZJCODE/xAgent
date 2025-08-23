import time
from typing import Optional, Dict, Any, List
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field


class MemoryType(Enum):
    """Types of memory supported by the system."""

    WORKING = "working"    # Short-term, task or session-specific memory.
                           # Examples:
                           #   - "Current task is to book a restaurant"
                           #   - "User mentioned they are planning a trip"

    PROFILE = "profile"    # Stored knowledge about users, preferences
                           # Examples:
                           #   - "User prefers Italian food"
                           #   - "User's birthday is in July"
                           #   - "User lives in Shanghai"

    EPISODIC = "episodic"  # Past interactions and experiences
                           # Examples:
                           #   - "Helped user find a nearby coffee shop on 2024-03-10"
                           #   - "User asked about weather last weekend"

    SEMANTIC = "semantic"  # Understanding of concepts and their relationships and world knowledge
                           # Examples:
                           #   - "A reservation is needed for popular restaurants"
                           #   - "Rainy weather may affect outdoor plans"
                           #   - "Morning is usually a busy time for traffic"

    PROCEDURAL = "procedural"  # how-to, tool usage patterns
                           # Examples:
                           #   - "To book a restaurant, you need to check availability"
                           #   - "Use the search function to find relevant documents"

class Memory(BaseModel):
    """Unified memory model for all types of memories."""
    
    id: str = Field(default_factory=lambda: str(uuid4()), description="UUID for the memory")
    user_id: str = Field(..., description="User identifier")
    type: MemoryType = Field(..., description="Type of memory")
    content: str = Field(..., description="The memory content as text")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Additional metadata")
    created_at: float = Field(default_factory=time.time, description="Creation timestamp")
    updated_at: Optional[float] = Field(None, description="Last update timestamp")
    embedding: Optional[List[float]] = Field(None, description="Vector embedding for semantic search")

    def touch(self) -> None:
        """Update the updated_at timestamp to now."""
        self.updated_at = time.time()

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the memory to a plain dict."""
        data = self.model_dump()
        data["type"] = self.type.value
        return data


__all__ = [
    "MemoryType",
    "Memory",
]