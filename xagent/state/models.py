"""Shared session, conversation, and job state models."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


JobStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
TranscriptRole = Literal["user", "assistant", "tool", "system"]


class TranscriptEntry(BaseModel):
    role: TranscriptRole
    content: str
    created_at: float = Field(default_factory=time.time)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class JobRecord(BaseModel):
    job_id: str
    conversation_id: str
    turn_id: str
    response_id: Optional[str] = None
    kind: str = "responses_task"
    status: JobStatus = "queued"
    result_text: Optional[str] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class ConversationRecord(BaseModel):
    conversation_id: str
    user_id: str
    transcript: List[TranscriptEntry] = Field(default_factory=list)
    task_summaries: List[str] = Field(default_factory=list)
    tool_summaries: List[str] = Field(default_factory=list)
    jobs: List[JobRecord] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class LiveSessionState(BaseModel):
    realtime_session_id: str
    conversation_id: str
    user_id: str
    status: Literal["active", "closed"] = "active"
    active_turn_id: Optional[str] = None
    active_response_id: Optional[str] = None
    active_job_id: Optional[str] = None
    interrupted: bool = False
    playback_active: bool = False
    buffered_text: str = ""
    buffered_audio_chunks: List[str] = Field(default_factory=list)
    buffered_frames: List[str] = Field(default_factory=list)
    provider_name: Optional[str] = None
    provider_session: Dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
