"""Pydantic request models for the HTTP server."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


MAX_INPUT_TEXT_CHARS = 65_536
MAX_ID_CHARS = 512
MAX_INPUT_ITEMS = 32


class ChatImageInput(BaseModel):
    """Optional image metadata accepted by API clients."""

    model_config = ConfigDict(extra="forbid")

    workspace_path: Optional[str] = None
    external_url: Optional[str] = None
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    blob_url: Optional[str] = None
    original_name: Optional[str] = None


class ChatAttachmentInput(BaseModel):
    """Optional workspace-backed attachment metadata accepted by API clients."""

    model_config = ConfigDict(extra="forbid")

    kind: Optional[str] = None
    path: Optional[str] = None
    workspace_path: Optional[str] = None
    blob_url: Optional[str] = None
    mime_type: Optional[str] = None
    file_name: Optional[str] = None
    original_name: Optional[str] = None
    caption: Optional[str] = None
    size_bytes: Optional[int] = None
    source_channel: Optional[str] = None
    source_message_id: Optional[str] = None
    source_resource_id: Optional[str] = None
    source_resource_type: Optional[str] = None


class ChatInput(BaseModel):
    """Final-only request body for the HTTP chat endpoint."""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=MAX_ID_CHARS)
    user_message: str = Field(max_length=MAX_INPUT_TEXT_CHARS)
    image_source: Optional[Union[str, List[str]]] = None
    images: Optional[List[ChatImageInput]] = Field(default=None, max_length=MAX_INPUT_ITEMS)
    attachments: Optional[List[ChatAttachmentInput]] = Field(default=None, max_length=MAX_INPUT_ITEMS)



class AgentInput(ChatInput):
    """Event request body for WebSocket chat."""

    stream: Optional[bool] = False


class ObserveInput(BaseModel):
    """Request body for observation endpoint."""

    context: str = Field(max_length=MAX_INPUT_TEXT_CHARS)
    source: Optional[str] = Field(default="environment", max_length=MAX_ID_CHARS)
    event_type: Optional[str] = Field(default="observation", max_length=MAX_ID_CHARS)
    metadata: Optional[Dict[str, Any]] = None
