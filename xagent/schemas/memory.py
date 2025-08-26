from curses import meta
from enum import Enum
from typing import List, Optional, Dict, Any
from pydantic import BaseModel

class MemoryType(Enum):
    """Types of memory supported by the system."""

    PROFILE = "profile"    # Stored knowledge about users, preferences
                           # Examples:
                           #   - "User prefers Italian food and vegetarian options"
                           #   - "User's birthday: July 15"
                           #   - "User lives in Shanghai, China"
                           #   - "User generally enjoys cheerful, informal conversations"
                           #   - "User's preferred contact method: email"
                           #   - "User is a premium member since 2022"

    EPISODIC = "episodic"  # Past interactions and experiences
                           # Examples:
                           #   - "On 2024-03-10, helped user find a nearby coffee shop"
                           #   - "User asked about weather last weekend"
                           #   - "User previously requested a refund for order #12345"
                           #   - "User shared positive feedback after using the booking service"
                           #   - "User reported an issue with login on 2024-04-01"



class MetaMemoryType(Enum):
    META = "meta"          # High-level summaries and insights derived from other memory types.

class MemoryPiece(BaseModel):
    """Schema for memory objects."""
    content: str
    type: MemoryType

class MemoryExtraction(BaseModel):
    """Schema for memory extraction results."""
    memories: List[MemoryPiece]

class MetaMemoryPiece(BaseModel):
    """Schema for memory objects."""
    content: str
    type: MetaMemoryType

class MetaMemory(BaseModel):
    """Schema for meta information about memory pieces."""
    contents: List[MetaMemoryPiece]

class QueryPreprocessResult(BaseModel):
    """Schema for query preprocessing results."""
    original_query: str
    rewritten_queries: List[str]
    keywords: List[str]
