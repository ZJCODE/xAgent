"""
MCP Protocol Integration Example

This example demonstrates how to integrate xAgent with Model Context Protocol (MCP)
servers for dynamic tool loading and management.
"""

import asyncio
from xagent.core import Agent
from xagent.utils.mcp_convertor import MCPTool

async def mcp_integration_example():
    """Connect to MCP server and use MCP tools with xAgent"""
    
    # Create agent with MCP tools
    agent = Agent(
        mcp_servers=["http://localhost:8001/mcp/"],  # Auto-refresh MCP tools
        model="gpt-4.1-mini"
    )
    
    # Use MCP tools automatically
    response = await agent.chat(
        user_message="Use the available MCP tools to help me",
        user_id="user123",
        session_id="session456"
    )
    print(response)

async def mcp_with_local_tools():
    """Combine MCP tools with local tools"""
    from xagent.utils.tool_decorator import function_tool
    
    # Local tool
    @function_tool()
    def local_calculator(a: int, b: int, operation: str) -> int:
        """Local calculator tool."""
        if operation == "add":
            return a + b
        elif operation == "multiply":
            return a * b
        elif operation == "subtract":
            return a - b
        elif operation == "divide":
            return a // b if b != 0 else 0
        return 0
    
    # Get MCP tools
    try:
        mcp_tool = MCPTool("http://localhost:8001/mcp/")
        mcp_tools = await mcp_tool.get_openai_tools()
    except Exception as e:
        print(f"Could not connect to MCP server: {e}")
        mcp_tools = []
    
    # Combine local and MCP tools
    all_tools = [local_calculator] + mcp_tools
    
    agent = Agent(
        tools=all_tools,
        model="gpt-4.1-mini",
        system_prompt="You have access to both local and MCP tools. Use them as needed."
    )
    
    response = await agent.chat(
        user_message="Calculate 15 * 23 using the calculator, and if you have access to other tools, show me what they can do",
        user_id="user123",
        session_id="session456"
    )
    print(response)


async def main():
    """Run MCP integration examples"""
    print("1. Basic MCP Integration:")
    try:
        await mcp_integration_example()
    except Exception as e:
        print(f"MCP integration failed: {e}")
    
    print("\n2. MCP with Local Tools:")
    await mcp_with_local_tools()
    

if __name__ == "__main__":
    asyncio.run(main())
