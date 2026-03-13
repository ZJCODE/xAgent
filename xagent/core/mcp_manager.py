"""
MCPManager — MCP server connection, caching, and tool discovery.

Responsibilities:
- Connecting to one or more MCP servers
- Caching discovered tool lists with a configurable TTL
- Returning a dict of ``{tool_name: callable_with_tool_spec}``
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

from tenacity import retry, stop_after_attempt, wait_exponential

from ..defaults import MCP_CACHE_TTL, RETRY_ATTEMPTS, RETRY_MIN_WAIT, RETRY_MAX_WAIT
from ..observability import observe


class MCPManager:
    """Manages MCP server connections, caching, and tool discovery."""

    def __init__(
        self,
        servers: List[str],
        cache_ttl: float = MCP_CACHE_TTL,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.servers = servers
        self.cache_ttl = cache_ttl
        self.logger = logger or logging.getLogger(__name__)
        self._tools: Dict[str, object] = {}
        self._last_updated: Optional[float] = None

    @property
    def tools(self) -> Dict[str, object]:
        """Current cached tool mapping ``{name: callable}``."""
        return self._tools

    @property
    def last_updated(self) -> Optional[float]:
        """Timestamp of the last successful refresh, or ``None``."""
        return self._last_updated

    @observe()
    @retry(
        stop=stop_after_attempt(RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=RETRY_MIN_WAIT, max=RETRY_MAX_WAIT),
    )
    async def refresh(self) -> None:
        """
        Fetch tools from all configured MCP servers, honouring the cache TTL.

        If the cache is still fresh this call is a no-op.  On a stale cache
        every server is queried; failures on individual servers are logged and
        skipped so that other servers continue to work.
        """
        now = time.time()
        if self._last_updated and (now - self._last_updated) < self.cache_ttl:
            return

        self._tools = {}
        for url in self.servers:
            try:
                from ..utils.mcp_convertor import MCPTool

                mt = MCPTool(url)
                mcp_tools = await mt.get_openai_tools()
                for tool in mcp_tools:
                    name = tool.tool_spec["name"]
                    if name not in self._tools:
                        self._tools[name] = tool
            except Exception as exc:
                self.logger.error(
                    "Failed to get tools from MCP server %s: %s", url, exc
                )

        self._last_updated = now
