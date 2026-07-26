from __future__ import annotations

__all__ = ["Agent", "AgentConfig", "ReplyType"]


def __getattr__(name: str):
    """Keep public imports lazy so low-level storage does not construct Agent."""
    if name == "Agent":
        from .agent import Agent

        return Agent
    if name in {"AgentConfig", "ReplyType"}:
        from .config import AgentConfig, ReplyType

        return {"AgentConfig": AgentConfig, "ReplyType": ReplyType}[name]
    raise AttributeError(name)
