"""Simple planning helpers for task-oriented Responses execution."""

from __future__ import annotations

from .models import TaskPlan


class TaskPlanner:
    """Lightweight heuristic planner used before execution."""

    BACKGROUND_HINTS = (
        "search",
        "research",
        "analyze",
        "document",
        "file",
        "plan",
        "workflow",
        "browse",
        "mcp",
        "compare",
    )

    def create_plan(self, user_input: str) -> TaskPlan:
        normalized = (user_input or "").strip()
        lowered = normalized.lower()
        requires_background = any(token in lowered for token in self.BACKGROUND_HINTS)

        steps = ["Interpret the user request"]
        if requires_background:
            steps.extend(
                [
                    "Gather external context or tool results",
                    "Run multi-step reasoning",
                    "Return a summarized answer",
                ]
            )
        else:
            steps.append("Return a direct response")

        return TaskPlan(
            summary=normalized[:120] or "empty task",
            steps=steps,
            requires_background=requires_background,
        )
