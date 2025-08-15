import asyncio
from fastmcp import Client

client = Client("http://127.0.0.1:8001/mcp/")

async def main():
    async with client:
        # Basic server interaction
        await client.ping()
        # List available operations
        tools = await client.list_tools()
        print("Available tools\n")
        print_tools(tools)
        resources = await client.list_resources()
        print("Available resources\n")
        print_resources(resources)
        prompts = await client.list_prompts()
        print("Available prompts\n")
        print_prompts(prompts)

        # Execute Tools
        result = await client.call_tool("roll_dice", {"n_dice": 2})
        print("Tool result:", result)
        
        # result = await client.call_tool("add_numbers", {"a": 5, "b": 10})
        # print("Tool result:", result)
        
        # Access Resource
        content = await client.read_resource("data://config")
        print("Resource result:", content)
        # Execute Prompt
        messages = await client.get_prompt("analyze_data", {"data_points": [1, 2, 3]})
        print("Prompt result:", messages)
        

def print_tools(tools):
    for tool in tools:
        print(f"Tool: {tool.name}")
        print(f"Description: {tool.description}")
        if tool.inputSchema:
            print(f"Parameters: {tool.inputSchema}")
        # Access tags and other metadata
        if hasattr(tool, '_meta') and tool._meta:
            fastmcp_meta = tool._meta.get('_fastmcp', {})
            print(f"Tags: {fastmcp_meta.get('tags', [])}")
        print("-" * 40 + "\n")


def print_resources(resources):
    for resource in resources:
        print(f"Resource URI: {resource.uri}")
        print(f"Name: {resource.name}")
        print(f"Description: {resource.description}")
        print(f"MIME Type: {resource.mimeType}")
        # Access tags and other metadata
        if hasattr(resource, '_meta') and resource._meta:
            fastmcp_meta = resource._meta.get('_fastmcp', {})
            print(f"Tags: {fastmcp_meta.get('tags', [])}")
        print("-" * 40 + "\n")

def print_prompts(prompts):
    for prompt in prompts:
        print(f"Prompt: {prompt.name}")
        print(f"Description: {prompt.description}")
        if prompt.arguments:
            print(f"Arguments: {[arg.name for arg in prompt.arguments]}")
        # Access tags and other metadata
        if hasattr(prompt, '_meta') and prompt._meta:
            fastmcp_meta = prompt._meta.get('_fastmcp', {})
            print(f"Tags: {fastmcp_meta.get('tags', [])}")
        print("-" * 40 + "\n")

asyncio.run(main())