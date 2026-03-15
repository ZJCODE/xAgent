import asyncio
import logging
import time
from typing import List, Optional, Union

from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import AgentConfig


logger = logging.getLogger(__name__)


class ToolManager:
    """Manages tool registration, MCP server integration, and tool spec caching.

    All tools (local, MCP, sub-agent) live in a single ``_tools`` dict.
    MCP tools are tagged with ``_source = "mcp"`` so they can be filtered
    via the ``mcp_tools`` property when needed (e.g. CLI display).
    """

    def __init__(
        self,
        tools: Optional[List] = None,
        mcp_servers: Optional[Union[str, List[str]]] = None,
    ):
        # Unified tool registry — local, MCP, and sub-agent tools all go here
        self._tools: dict = {}

        # MCP state
        self._mcp_servers: List[str] = self._normalize_mcp_servers(mcp_servers)
        self._mcp_clients: dict = {}  # URL → MCPTool, cached across refreshes
        self._mcp_initialized: bool = False
        self._mcp_last_refresh: Optional[float] = None
        self._mcp_cache_ttl: int = AgentConfig.MCP_CACHE_TTL

        # Tool-spec cache with dirty flag
        self._tool_specs_cache: Optional[list] = None
        self._cache_dirty: bool = True

        # Register initial local tools
        self.register_tools(tools or [])

    # ---- Public properties (backward-compatible) ----

    @property
    def tools(self) -> dict:
        """All non-MCP tools (local + sub-agent)."""
        return {k: v for k, v in self._tools.items() if getattr(v, "_source", None) != "mcp"}

    @property
    def mcp_tools(self) -> dict:
        """MCP-sourced tools only."""
        return {k: v for k, v in self._tools.items() if getattr(v, "_source", None) == "mcp"}

    @property
    def mcp_servers(self) -> List[str]:
        return self._mcp_servers

    # ---- Tool registration ----

    def register_tools(self, tools: Optional[list]) -> None:
        """Register tool functions. Each must be async and have a ``tool_spec``."""
        for fn in tools or []:
            if not asyncio.iscoroutinefunction(fn):
                raise TypeError(f"Tool function '{fn.tool_spec['name']}' must be async.")
            if fn.tool_spec["name"] not in self._tools:
                self._tools[fn.tool_spec["name"]] = fn
        self._cache_dirty = True

    # ---- MCP lifecycle ----

    async def ensure_mcp_ready(self) -> None:
        """Lazy-init MCP tools on first call; refresh when TTL expires."""
        if not self._mcp_servers:
            return

        now = time.monotonic()
        if self._mcp_initialized and self._mcp_last_refresh is not None:
            if (now - self._mcp_last_refresh) < self._mcp_cache_ttl:
                return

        # Clear previous MCP entries from the unified dict
        mcp_keys = [k for k, v in self._tools.items() if getattr(v, "_source", None) == "mcp"]
        for k in mcp_keys:
            del self._tools[k]

        for url in self._mcp_servers:
            await self._fetch_mcp_tools(url)

        self._mcp_initialized = True
        self._mcp_last_refresh = time.monotonic()
        self._cache_dirty = True

    @retry(
        stop=stop_after_attempt(AgentConfig.RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=AgentConfig.RETRY_MIN_WAIT, max=AgentConfig.RETRY_MAX_WAIT),
    )
    async def _fetch_mcp_tools(self, url: str) -> None:
        """Fetch tools from a single MCP server. Retries on transient failures."""
        try:
            from ...utils.mcp_convertor import MCPTool

            if url not in self._mcp_clients:
                self._mcp_clients[url] = MCPTool(url)
            mt = self._mcp_clients[url]

            mcp_tools = await mt.get_openai_tools()
            for tool in mcp_tools:
                name = tool.tool_spec["name"]
                if name not in self._tools:
                    tool._source = "mcp"
                    self._tools[name] = tool
        except Exception as e:
            logger.error("Failed to get tools from MCP server %s: %s", url, e)
            raise  # let tenacity retry

    # ---- Tool lookup ----

    def get_tool(self, name: str):
        """Look up a tool by name."""
        return self._tools.get(name)

    # ---- Tool spec cache ----

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

    # ---- Helpers ----

    @staticmethod
    def _normalize_mcp_servers(mcp_servers: Optional[Union[str, List[str]]]) -> List[str]:
        if not mcp_servers:
            return []
        if isinstance(mcp_servers, str):
            return [mcp_servers]
        return list(mcp_servers)
