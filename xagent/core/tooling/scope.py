"""Call-scope context shared between the tool executor and tool functions.

A leaf module so tool modules can read the caller's identity without pulling
in the executor (which imports back into ``xagent.tools``).
Set/reset happen inside one awaited ``handle_tool_calls`` invocation, so the
value is stable within a single turn iteration.
"""
from __future__ import annotations

from contextvars import ContextVar

tool_call_scope: ContextVar = ContextVar("tool_call_scope", default=("", None, None, ""))


def current_tool_call_scope() -> tuple:
    """Return ``(user_id, channel, room_name, inbox_kind)`` of the running call."""
    return tool_call_scope.get()
