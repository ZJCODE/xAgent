"""Request and result models for orchestration."""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field


ExecutionMode = Literal["realtime_tool", "responses", "background"]


class TurnInput(BaseModel):
    text: str = ""
    image_source: Optional[Any] = None
    audio_chunks: list[str] = Field(default_factory=list)
    frames: list[str] = Field(default_factory=list)
    requested_tool: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class OrchestratorContext(BaseModel):
    user_id: str
    conversation_id: str
    turn_id: str
    realtime_session_id: Optional[str] = None
    history_count: int = 16
    max_iter: int = 10
    max_concurrent_tools: int = 10
    allow_background: bool = True
    force_background: bool = False
    stream: bool = False
    enable_memory: bool = False
    confirmed_tools: set[str] = Field(default_factory=set)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class OrchestratorResult(BaseModel):
    mode: ExecutionMode
    conversation_id: str
    turn_id: str
    response_id: Optional[str] = None
    job_id: Optional[str] = None
    output: Optional[Any] = None
    output_text: Optional[str] = None
    tool_name: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
