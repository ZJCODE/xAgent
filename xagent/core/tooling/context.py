"""Execution-local metadata exposed to built-in tools."""
from __future__ import annotations

from contextvars import ContextVar, Token


_tool_call_id: ContextVar[str] = ContextVar("xagent_tool_call_id", default="")


def current_tool_call_id() -> str:
    return _tool_call_id.get()


def set_tool_call_id(call_id: str) -> Token[str]:
    return _tool_call_id.set(str(call_id or ""))


def reset_tool_call_id(token: Token[str]) -> None:
    _tool_call_id.reset(token)
