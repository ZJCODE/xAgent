"""Responses-tier tool wrappers and helpers."""

from __future__ import annotations

from typing import Any, Callable, Iterable, List, Optional


class RegisteredTool:
    """Runtime wrapper for tiered tool metadata."""

    def __init__(self, func: Callable[..., Any]):
        self.func = func
        self.name = getattr(getattr(func, "tool_spec", {}), "get", lambda *_: None)("name")
        if not self.name:
            self.name = getattr(func, "__name__", "tool")
        self.description = getattr(getattr(func, "tool_spec", {}), "get", lambda *_: None)(
            "description"
        ) or ""
        self.tier = getattr(func, "tool_tier", "responses")
        self.timeout_seconds = getattr(func, "tool_timeout_seconds", None)
        self.requires_confirmation = getattr(func, "tool_requires_confirmation", False)


class RealtimeTool(RegisteredTool):
    tier = "realtime"


class ResponsesTool(RegisteredTool):
    tier = "responses"


def get_tool_name(func: Callable[..., Any]) -> str:
    return RegisteredTool(func).name


def get_tool_tier(func: Callable[..., Any]) -> str:
    return getattr(func, "tool_tier", "responses")


def split_tools(tools: Optional[Iterable[Callable[..., Any]]]) -> tuple[List[Callable[..., Any]], List[Callable[..., Any]]]:
    realtime_tools: List[Callable[..., Any]] = []
    responses_tools: List[Callable[..., Any]] = []
    for tool in tools or []:
        if get_tool_tier(tool) == "realtime":
            realtime_tools.append(tool)
        else:
            responses_tools.append(tool)
    return realtime_tools, responses_tools
