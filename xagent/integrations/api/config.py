"""Runtime configuration for the api channel adapter."""

from __future__ import annotations

from dataclasses import dataclass

from ...core.config import AgentConfig


@dataclass(frozen=True)
class ChatLimits:
    max_concurrent_chats: int = AgentConfig.DEFAULT_HTTP_MAX_CONCURRENT_CHATS
    chat_queue_timeout: float = AgentConfig.DEFAULT_HTTP_QUEUE_TIMEOUT
    chat_timeout: float = AgentConfig.DEFAULT_HTTP_CHAT_TIMEOUT
