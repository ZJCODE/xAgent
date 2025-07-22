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
    word: str = Field(..., description="用户查询的词汇")
    explanation: str = Field(..., description="词汇的解释")
    example_sentences: List[str] = Field(default_factory=list, description="例句列表")
    difficulty_level: DifficultyLevel = Field(default=DifficultyLevel.INTERMEDIATE, description="词汇难度级别")

class VocabularyRecord(BaseVocabularyRecord):
    user_id: str = Field(..., description="用户ID")
    create_timestamp: float = Field(..., description="创建时间戳")
    last_reviewed_timestamp: Optional[float] = Field(default=None, description="上次复习时间戳")
    familiarity: int = Field(0, ge=0, le=10, description="词汇熟悉度，0-10")
    extra: Optional[Dict[str, str]] = Field(default=None, description="扩展字段，可存储额外信息")

# 示例：
# VocabularyRecord(
#     word="apple",
#     explanation="A fruit",
#     user_id="user123",
#     create_timestamp=1633072800,
#     last_reviewed_timestamp=1633159200,
#     familiarity=5,
#     difficulty_level=DifficultyLevel.BEGINNER,
#     example_sentences=["I eat an apple every day.", "The apple is red and sweet."],
#     image_url="https://example.com/apple.jpg",
#     extra={"part_of_speech": "noun","image_url": "https://example.com/apple.jpg"}
# )