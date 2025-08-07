from pydantic import BaseModel, Field
from typing import Optional, Dict, List
from enum import Enum

class DifficultyLevel(str, Enum):
    """Enumeration for vocabulary difficulty levels."""
    BEGINNER = "beginner"        # Easy (A1-A2)
    INTERMEDIATE = "intermediate" # Intermediate (B1-B2)
    ADVANCED = "advanced"        # Advanced (C1-C2)
    EXPERT = "expert"            # Expert

class BaseVocabularyRecord(BaseModel):
    """Vocabulary record base model."""
    word: str = Field(..., description="The vocabulary word in lowercase")
    explanation: str = Field(..., description="The explanation of the word")
    example_sentences: List[str] = Field(default_factory=list, description="The example sentences")
    difficulty_level: DifficultyLevel = Field(default=DifficultyLevel.INTERMEDIATE, description="The difficulty level of the word")

class VocabularyRecord(BaseVocabularyRecord):
    """Vocabulary record model."""
    user_id: str = Field(..., description="The user ID")
    create_timestamp: Optional[float] = Field(default=None, description="The creation timestamp")
    update_timestamp: Optional[float] = Field(default=None, description="The last update timestamp")
    last_reviewed_timestamp: Optional[float] = Field(default=None, description="The last reviewed timestamp")
    familiarity: int = Field(0, ge=0, le=10, description="The familiarity level of the word, from 0 to 10")
    extra: Optional[Dict[str, str]] = Field(default=None, description="The extra fields for storing additional information")
