from curses import meta
from enum import Enum
from typing import List, Optional, Dict, Any
from pydantic import BaseModel

class MemoryType(Enum):
    """Types of memory supported by the system."""

    WORKING = "working"    # Short-term, task or session-specific memory.
                           # Examples:
                           #   - "Current task: schedule a meeting with Dr. Smith at 3 PM"
                           #   - "User just provided a new delivery address for this order"
                           #   - "Session context: user is comparing two products"
                           #   - "User mentioned they are planning a trip to Paris next month"

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

    SEMANTIC = "semantic"  # General world knowledge, facts, concepts, and their relationships (semantic memory)
                           # Examples:
                           #   - "Paris is the capital of France"
                           #   - "A reservation is required for popular restaurants during weekends"
                           #   - "Water boils at 100°C under standard atmospheric pressure"
                           #   - "Rainy weather may affect outdoor plans and traffic conditions"
                           #   - "Express shipping is faster but more expensive than standard shipping"
                           #   - "A valid ID is needed for hotel check-in"
                           #   - "Dogs are mammals"

    PROCEDURAL = "procedural"  # How-to, tool usage patterns
                           # Examples:
                           #   - "To book a restaurant, check availability, select time, and confirm reservation"
                           #   - "Use the search function to find relevant documents"
                           #   - "Reset password by clicking 'Forgot Password' and following the instructions"
                           #   - "To cancel an order, go to 'My Orders' and select 'Cancel'"

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
