"""Provider-neutral realtime event models."""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field


ClientEventType = Literal[
    "session.start",
    "input.text",
    "input.audio.chunk",
    "input.image.frame",
    "turn.commit",
    "interrupt",
    "session.close",
]

ServerEventType = Literal[
    "ack",
    "partial_text",
    "partial_audio",
    "turn.started",
    "turn.completed",
    "job.started",
    "job.progress",
    "job.completed",
    "job.failed",
    "session.state",
]


class RealtimeClientEvent(BaseModel):
    type: ClientEventType
    user_id: Optional[str] = None
    conversation_id: Optional[str] = None
    realtime_session_id: Optional[str] = None
    turn_id: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)


class RealtimeServerEvent(BaseModel):
    type: ServerEventType
    conversation_id: Optional[str] = None
    realtime_session_id: Optional[str] = None
    turn_id: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
