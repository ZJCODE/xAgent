"""HTTP server package for xAgent.

Exposes the FastAPI-based :class:`AgentHTTPServer` and the request models used
by its routes.
"""
from __future__ import annotations

from .app import AgentHTTPServer
from .console import ConsoleHTTPServer
from .models import (
    AgentInput,
    ChatAttachmentInput,
    ChatImageInput,
    ChatInput,
    IdentityInput,
    ObserveInput,
    SkillCreateInput,
    SkillStateInput,
    SkillWriteInput,
    WorkspaceWriteInput,
)

__all__ = [
    "AgentHTTPServer",
    "ConsoleHTTPServer",
    "AgentInput",
    "ChatAttachmentInput",
    "ChatImageInput",
    "ChatInput",
    "IdentityInput",
    "ObserveInput",
    "SkillCreateInput",
    "SkillStateInput",
    "SkillWriteInput",
    "WorkspaceWriteInput",
]
