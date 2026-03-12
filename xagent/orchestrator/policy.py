"""Tool permission and confirmation policy."""

from __future__ import annotations

from typing import Any

from .models import OrchestratorContext


class ToolPolicy:
    """Evaluates whether a tool can run in the current context."""

    def check(self, tool: Any, context: OrchestratorContext) -> None:
        tool_name = getattr(getattr(tool, "tool_spec", {}), "get", lambda *_: None)("name")
        tool_name = tool_name or getattr(tool, "__name__", "tool")

        if getattr(tool, "tool_requires_confirmation", False) and tool_name not in context.confirmed_tools:
            raise PermissionError(f"Tool `{tool_name}` requires confirmation before execution.")
