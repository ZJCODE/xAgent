import asyncio
import logging
import time
from typing import List, Optional, Union

from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import AgentConfig


logger = logging.getLogger(__name__)


class ToolManager:
    """Manages tool registration, MCP server integration, and tool spec caching."""

    def __init__(
        self,
        tools: Optional[List] = None,
        mcp_servers: Optional[Union[str, List[str]]] = None,
    ):
        self.tools: dict = {}
        self.mcp_tools: dict = {}
        self.mcp_tools_last_updated: Optional[float] = None
        self.mcp_cache_ttl = AgentConfig.MCP_CACHE_TTL

        # Tool specs cache
        self._tool_specs_cache: Optional[list] = None
        self._tools_last_updated: Optional[float] = None

        self.mcp_servers = self._normalize_mcp_servers(mcp_servers)

        # Register initial tools
        self.register_tools(tools or [])

    def register_tools(self, tools: Optional[list]) -> None:
        """Register tool functions with the agent. Each must be async and have a tool_spec."""
        for fn in tools or []:
            if not asyncio.iscoroutinefunction(fn):
                raise TypeError(f"Tool function '{fn.tool_spec['name']}' must be async.")
            if fn.tool_spec['name'] not in self.tools:
                self.tools[fn.tool_spec['name']] = fn

        # Invalidate cache after registering new tools
        self._tool_specs_cache = None

    @retry(
        stop=stop_after_attempt(AgentConfig.RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=AgentConfig.RETRY_MIN_WAIT, max=AgentConfig.RETRY_MAX_WAIT)
    )
    async def register_mcp_servers(self, mcp_servers: Optional[Union[str, list]]) -> None:
        """Register tools from MCP servers, updating the local cache if needed."""
        now = time.time()
        if self.mcp_tools_last_updated and (now - self.mcp_tools_last_updated) < self.mcp_cache_ttl:
            return

        self.mcp_tools = {}
        if isinstance(mcp_servers, str):
            mcp_servers = [mcp_servers]
        for url in mcp_servers or []:
            try:
                from ...utils.mcp_convertor import MCPTool

                mt = MCPTool(url)
                mcp_tools = await mt.get_openai_tools()
                for tool in mcp_tools:
                    if tool.tool_spec['name'] not in self.mcp_tools:
                        self.mcp_tools[tool.tool_spec['name']] = tool
            except Exception as e:
                logger.error("Failed to get tools from MCP server %s: %s", url, e)
                continue

        self.mcp_tools_last_updated = now

    def get_tool(self, name: str):
        """Look up a tool by name from local tools or MCP tools."""
        return self.tools.get(name) or self.mcp_tools.get(name)

    @property
    def cached_tool_specs(self) -> Optional[list]:
        """Returns the cached tool specifications, rebuilding if necessary."""
        if self._should_rebuild_cache():
            self._rebuild_tool_cache()
        return self._tool_specs_cache

    def _should_rebuild_cache(self) -> bool:
        if self._tool_specs_cache is None:
            return True
        if self.mcp_tools_last_updated and (
            self._tools_last_updated is None
            or self.mcp_tools_last_updated > self._tools_last_updated
        ):
            return True
        return False

    def _rebuild_tool_cache(self) -> None:
        all_tools = list(self.tools.values()) + list(self.mcp_tools.values())
        self._tool_specs_cache = [fn.tool_spec for fn in all_tools] if all_tools else None
        self._tools_last_updated = time.time()

    @staticmethod
    def _normalize_mcp_servers(mcp_servers: Optional[Union[str, List[str]]]) -> List[str]:
        if not mcp_servers:
            return []
        if isinstance(mcp_servers, str):
            return [mcp_servers]
        return list(mcp_servers)
