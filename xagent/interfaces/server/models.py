"""Pydantic request models for the HTTP server."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict


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


class ConfigInput(BaseModel):
    """Request body for updating config.yaml."""

    model_config = ConfigDict(extra="forbid")

    config: str


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
    expected_revision: Optional[str] = None


class SkillEntryCreateInput(BaseModel):
    """Request body for creating a file or directory inside a skill package."""

    model_config = ConfigDict(extra="forbid")

    parent_path: str
    name: str
    kind: str
    content: str = ""


class SkillEntryMoveInput(BaseModel):
    """Request body for renaming or moving a skill package entry."""

    model_config = ConfigDict(extra="forbid")

    path: str
    new_parent_path: str
    new_name: str
    expected_revision: Optional[str] = None


class SkillStateInput(BaseModel):
    """Request body for enabling or disabling a skill."""

    model_config = ConfigDict(extra="forbid")

    name: str
    enabled: bool

class TaskUpdateInput(BaseModel):
    """Request body for patching a scheduled task."""

    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = None
    content: Optional[str] = None
    task_type: Optional[str] = None
    run_at: Optional[str] = None
    delay_seconds: Optional[int] = None
    recurrence: Optional[List[Dict[str, Any]]] = None
    interval_seconds: Optional[int] = None
    duration_seconds: Optional[int] = None
    start_at: Optional[str] = None
    end_at: Optional[str] = None


class TaskCreateInput(BaseModel):
    """Request body for creating an api-channel scheduled task from Web/HTTP."""

    model_config = ConfigDict(extra="forbid")

    task_type: str
    content: str
    title: Optional[str] = None
    run_at: Optional[str] = None
    delay_seconds: Optional[int] = None
    recurrence: Optional[List[Dict[str, Any]]] = None
    interval_seconds: Optional[int] = None
    duration_seconds: Optional[int] = None
    start_at: Optional[str] = None
    end_at: Optional[str] = None
    channel: Optional[str] = "api"
    user_id: Optional[str] = None
    target: Optional[Dict[str, Any]] = None


class TaskDuplicateInput(BaseModel):
    """Overrides and a fresh schedule for duplicating a completed task."""

    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = None
    content: Optional[str] = None
    task_type: Optional[str] = None
    run_at: Optional[str] = None
    delay_seconds: Optional[int] = None
    recurrence: Optional[List[Dict[str, Any]]] = None
    interval_seconds: Optional[int] = None
    duration_seconds: Optional[int] = None
    start_at: Optional[str] = None
    end_at: Optional[str] = None
