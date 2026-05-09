import asyncio
from typing import List, Optional


class ToolManager:
    """Manages tool registration and tool spec caching."""

    def __init__(
        self,
        tools: Optional[List] = None,
    ):
        self._tools: dict = {}

        self._tool_specs_cache: Optional[list] = None
        self._cache_dirty: bool = True

        self.register_tools(tools or [])

    @property
    def tools(self) -> dict:
        """Registered tools keyed by tool name."""
        return dict(self._tools)

    def register_tools(self, tools: Optional[list]) -> None:
        """Register tool functions. Each must be async and have a ``tool_spec``."""
        for fn in tools or []:
            if not asyncio.iscoroutinefunction(fn):
                raise TypeError(f"Tool function '{fn.tool_spec['name']}' must be async.")
            if fn.tool_spec["name"] not in self._tools:
                self._tools[fn.tool_spec["name"]] = fn
        self._cache_dirty = True

    def get_tool(self, name: str):
        """Look up a tool by name."""
        return self._tools.get(name)

    @property
    def cached_tool_specs(self) -> Optional[list]:
        """Returns cached tool specifications, rebuilding only when dirty."""
        if self._cache_dirty:
            self._rebuild_tool_cache()
        return self._tool_specs_cache

    def _rebuild_tool_cache(self) -> None:
        tools = list(self._tools.values())
        self._tool_specs_cache = [fn.tool_spec for fn in tools] if tools else None
        self._cache_dirty = False
