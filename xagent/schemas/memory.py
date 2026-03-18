from enum import Enum
from typing import List

from pydantic import BaseModel, Field


class MemoryType(str, Enum):
    """Types of long-term memory supported by the system."""

    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    SOCIAL = "social"
    SELF = "self"


class MemoryPiece(BaseModel):
    """A single durable memory item extracted from transcript chunks."""

    content: str
    type: MemoryType


class MemoryExtraction(BaseModel):
    """Structured output returned by the memory extraction model."""

    memories: List[MemoryPiece] = Field(default_factory=list)
