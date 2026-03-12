"""Combine local tools with MCP tools when an MCP server is available."""

import asyncio

from xagent.components import MessageStorageLocal
from xagent.core import Agent
from xagent.utils import MCPTool, function_tool


MCP_SERVER_URL = "http://localhost:8001/mcp/"


@function_tool()
def local_calculator(a: int, b: int, operation: str) -> int:
    """Run a simple arithmetic operation."""
    operations = {
        "add": a + b,
        "subtract": a - b,
        "multiply": a * b,
        "divide": a // b if b else 0,
    }
    return operations.get(operation, 0)


async def main():
    mcp_servers = []

    try:
        await MCPTool(MCP_SERVER_URL).get_openai_tools()
        mcp_servers = [MCP_SERVER_URL]
        print(f"Connected to MCP server at {MCP_SERVER_URL}")
    except Exception as exc:
        print(f"MCP server unavailable, running local-only fallback: {exc}")

    agent = Agent(
        model="gpt-4.1-mini",
        tools=[local_calculator],
        mcp_servers=mcp_servers,
        system_prompt="Use the local calculator for arithmetic and MCP tools when they are available.",
        message_storage=MessageStorageLocal(),
    )

    response = await agent.chat(
        user_message=(
            "Use the calculator to multiply 15 by 23. "
            "If MCP tools are available, briefly mention what else you can access."
        ),
        user_id="demo_user",
        session_id="mcp_demo",
    )
    print(response)


if __name__ == "__main__":
    asyncio.run(main())
