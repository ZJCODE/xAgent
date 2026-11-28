import asyncio
from typing import Dict, FrozenSet, List, Optional


CHANNEL_INCOMPATIBLE_TOOLS: Dict[str, FrozenSet[str]] = {
    "voice": frozenset({"attach_artifact"}),
}


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
            tool_name = self._tool_spec_name(fn.tool_spec)
            if not asyncio.iscoroutinefunction(fn):
                raise TypeError(f"Tool function '{tool_name}' must be async.")
            if tool_name not in self._tools:
                self._tools[tool_name] = fn
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

    def select_tool_names(self, *, channel: Optional[str] = None) -> List[str]:
        excluded = CHANNEL_INCOMPATIBLE_TOOLS.get((channel or "").strip().lower(), frozenset())
        if not excluded:
            return list(self._tools)
        return [name for name in self._tools if name not in excluded]

    def select_tool_specs(self, *, channel: Optional[str] = None) -> Optional[list]:
        specs = self.cached_tool_specs
        if not specs:
            return None
        excluded = CHANNEL_INCOMPATIBLE_TOOLS.get((channel or "").strip().lower(), frozenset())
        if not excluded:
            return specs
        filtered = [spec for spec in specs if self._tool_spec_name(spec) not in excluded]
        return filtered or None

    @staticmethod
    def _tool_spec_name(tool_spec: dict) -> str:
        return tool_spec.get("function", {}).get("name") or tool_spec.get("name")
