"""Pydantic request models for the HTTP server."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict

from ..core.config import AgentConfig


class ChatImageInput(BaseModel):
    """Optional image metadata accepted by API clients."""

    model_config = ConfigDict(extra="ignore")

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

    model_config = ConfigDict(extra="ignore")

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

    user_id: str
    user_message: str
    image_source: Optional[Union[str, List[str]]] = None
    images: Optional[List[ChatImageInput]] = None
    attachments: Optional[List[ChatAttachmentInput]] = None
    history_count: Optional[int] = AgentConfig.DEFAULT_HISTORY_COUNT
    max_iter: Optional[int] = AgentConfig.DEFAULT_MAX_ITER
    max_concurrent_tools: Optional[int] = AgentConfig.DEFAULT_MAX_CONCURRENT_TOOLS
    enable_memory: Optional[bool] = True


class AgentInput(ChatInput):
    """Event request body for WebSocket chat."""

    stream: Optional[bool] = False


class ObserveInput(BaseModel):
    """Request body for observation endpoint."""

    context: str
    source: Optional[str] = "environment"
    event_type: Optional[str] = "observation"
    metadata: Optional[Dict[str, Any]] = None


class IdentityInput(BaseModel):
    """Request body for updating identity.md."""

    identity: str


class WorkspaceWriteInput(BaseModel):
    """Request body for writing a text file in workspace/."""

    model_config = ConfigDict(extra="forbid")

    path: str
    content: str
    create_parents: bool = True


class SkillCreateInput(BaseModel):
    """Request body for creating a new Agent Skill package."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    body: str = ""
    license: Optional[str] = None
    compatibility: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    allowed_tools: Optional[str] = None


class SkillWriteInput(BaseModel):
    """Request body for writing a text file in skills/."""

    model_config = ConfigDict(extra="forbid")

    path: str
    content: str
    create_parents: bool = True


class SkillStateInput(BaseModel):
    """Request body for enabling or disabling a skill."""

    model_config = ConfigDict(extra="forbid")

    name: str
    enabled: bool