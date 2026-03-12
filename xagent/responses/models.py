"""Models for task execution in the Responses engine."""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class TaskContext(BaseModel):
    user_id: str
    conversation_id: str
    turn_id: str
    realtime_session_id: Optional[str] = None
    image_source: Optional[Any] = None
    history_count: int = 16
    max_iter: int = 10
    max_concurrent_tools: int = 10
    enable_memory: bool = False
    stream: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TaskPlan(BaseModel):
    summary: str
    steps: List[str] = Field(default_factory=list)
    requires_background: bool = False


class TaskResult(BaseModel):
    response_id: str = Field(default_factory=lambda: f"resp_{uuid.uuid4().hex[:10]}")
    conversation_id: str
    turn_id: str
    output: Any
    output_text: str
    plan: Optional[TaskPlan] = None
    tool_summaries: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)
