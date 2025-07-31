from fastmcp import Client
from typing import Any, Callable, List

class MCPTool:
    """
    MCPTool 对象，初始化时传入 fastmcp 服务的 URL，可获取 openai 规范的 tools。
    """

    def __init__(self, url: str):
        """
        Args:
            url: fastmcp 服务的 URL
        """
        self.client = Client(url)

    def _get_attr_or_key(self, obj, key, default=None):
        # Try attribute first, then dict key, else default
        if hasattr(obj, key):
            return getattr(obj, key)
        elif isinstance(obj, dict):
            return obj.get(key, default)
        else:
            return default

    def _convert_to_openai_tool(self, mcp_tool: Any) -> Callable:
        """
        将 MCP tool 对象或 dict 转换为 OpenAI tool 规范的异步函数，并带有 tool_spec 属性。
        """
        name = self._get_attr_or_key(mcp_tool, "name")
        description = self._get_attr_or_key(mcp_tool, "description", "")
        input_schema = self._get_attr_or_key(mcp_tool, "inputSchema", {})
        input_schema.update({"additionalProperties": False})

        async def openai_tool_func(**kwargs):
            if self.client is None:
                raise RuntimeError("MCPTool: client is not set.")
            async with self.client:
                result = await self.client.call_tool(name, kwargs)
                return result.data

        openai_tool_func.tool_spec = {
            "type": "function",
            "name": name,
            "description": description,
            "parameters": input_schema
        }
        return openai_tool_func

    async def get_openai_tools(self) -> List[Callable]:
        """
        获取 openai 规范的 tool 函数列表（带 tool_spec 属性）。
        Returns:
            List[Callable]: 每个函数带有 tool_spec 属性
        """
        async with self.client:
            tools = await self.client.list_tools()
            return [self._convert_to_openai_tool(tool) for tool in tools]

if __name__ == "__main__":
    import asyncio

    mcp_tool = MCPTool("http://127.0.0.1:8001/mcp/")

    async def main():
        tools = await mcp_tool.get_openai_tools()
        for tool in tools:
            print(f"Tool: {tool.tool_spec['name']}, Description: {tool.tool_spec['description']}")
        result = await tools[0](n_dice=2)
        print(f"Result: {result}")

    asyncio.run(main())