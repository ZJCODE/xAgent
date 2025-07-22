from pydantic import BaseModel, Field
from typing import Optional, Dict

class VocabularyRecord(BaseModel):
    word: str = Field(..., description="用户查询的词汇")
    explanation: str = Field(..., description="词汇的解释")
    user_id: str = Field(..., description="用户ID")
    create_timestamp: float = Field(..., description="创建时间戳")
    last_reviewed_timestamp: Optional[float] = Field(default=None, description="上次复习时间戳")
    familiarity: int = Field(0, ge=0, le=10, description="词汇熟悉度，0-10")
    image_url: Optional[str] = Field(default=None, description="相关图片链接")
    extra: Optional[Dict[str, str]] = Field(default=None, description="扩展字段，可存储额外信息")

# 示例：
# VocabularyRecord(
#     word="apple",
#     explanation="A fruit",
#     user_id="user123",
#     create_timestamp=1633072800,
#     last_reviewed_timestamp=1633159200,
#     familiarity=5,
#     image_url="https://example.com/apple.jpg",
#     extra={"part_of_speech": "noun"}
# )