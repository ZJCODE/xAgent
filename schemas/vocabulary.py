from pydantic import BaseModel, Field
from typing import Optional, Dict, List
from enum import Enum

class DifficultyLevel(str, Enum):
    """词汇难度级别枚举"""
    BEGINNER = "beginner"        # 初级 (A1-A2)
    INTERMEDIATE = "intermediate" # 中级 (B1-B2) 
    ADVANCED = "advanced"        # 高级 (C1-C2)
    EXPERT = "expert"            # 专家级

class BaseVocabularyRecord(BaseModel):
    """基础词汇记录模型"""
    word: str = Field(..., description="The vocabulary word")
    explanation: str = Field(..., description="The explanation of the word")
    example_sentences: List[str] = Field(default_factory=list, description="The example sentences")
    difficulty_level: DifficultyLevel = Field(default=DifficultyLevel.INTERMEDIATE, description="The difficulty level of the word")

class VocabularyRecord(BaseVocabularyRecord):
    user_id: str = Field(..., description="The user ID")
    create_timestamp: Optional[float] = Field(default=None, description="The creation timestamp")
    update_timestamp: Optional[float] = Field(default=None, description="The last update timestamp")
    last_reviewed_timestamp: Optional[float] = Field(default=None, description="The last reviewed timestamp")
    familiarity: int = Field(0, ge=0, le=10, description="The familiarity level of the word, from 0 to 10")
    extra: Optional[Dict[str, str]] = Field(default=None, description="The extra fields for storing additional information")
