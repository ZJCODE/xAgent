"""HTTP server package for xAgent.

Exposes the FastAPI-based :class:`AgentHTTPServer` and the request models used
by its routes.
"""
from __future__ import annotations

from .models import (
    AgentInput,
    ChatAttachmentInput,
    ChatImageInput,
    ChatInput,
    IdentityInput,
    ObserveInput,
    SkillCreateInput,
    SkillEntryCreateInput,
    SkillEntryMoveInput,
    SkillStateInput,
    SkillWriteInput,
    WorkspaceWriteInput,
)

__all__ = [
    "AgentHTTPServer",
    "AgentInput",
    "ChatAttachmentInput",
    "ChatImageInput",
    "ChatInput",
    "IdentityInput",
    "ObserveInput",
    "SkillCreateInput",
    "SkillEntryCreateInput",
    "SkillEntryMoveInput",
    "SkillStateInput",
    "SkillWriteInput",
    "WorkspaceWriteInput",
]


def __getattr__(name: str):
    if name == "AgentHTTPServer":
        from .app import AgentHTTPServer
        return AgentHTTPServer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
