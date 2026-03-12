"""Intent and complexity routing rules."""

from __future__ import annotations

from typing import Any, Iterable, Optional

from .models import ExecutionMode, OrchestratorContext, TurnInput


class IntentRouter:
    """Routes turns to realtime tools, foreground Responses, or background jobs."""

    COMPLEXITY_HINTS = (
        "search",
        "research",
        "analyze",
        "analysis",
        "document",
        "plan",
        "workflow",
        "compare",
        "browser",
        "browse",
        "file",
        "mcp",
        "api",
    )

    def route(
        self,
        turn: TurnInput,
        context: OrchestratorContext,
        available_tools: Optional[Iterable[Any]] = None,
    ) -> ExecutionMode:
        if turn.requested_tool:
            for tool in available_tools or []:
                tool_name = getattr(getattr(tool, "tool_spec", {}), "get", lambda *_: None)("name")
                if tool_name == turn.requested_tool and getattr(tool, "tool_tier", "responses") == "realtime":
                    return "realtime_tool"

        if context.force_background:
            return "background"

        lowered = (turn.text or "").lower()
        if any(token in lowered for token in self.COMPLEXITY_HINTS):
            return "background" if context.allow_background else "responses"

        return "responses"
