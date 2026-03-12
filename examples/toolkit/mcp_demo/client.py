import asyncio

from fastmcp import Client


client = Client("http://127.0.0.1:8001/mcp/")


async def main():
    async with client:
        await client.ping()

        tools = await client.list_tools()
        print("Available tools\n")
        print_tools(tools)

        resources = await client.list_resources()
        print("Available resources\n")
        print_resources(resources)

        prompts = await client.list_prompts()
        print("Available prompts\n")
        print_prompts(prompts)

        tool_result = await client.call_tool("roll_dice", {"n_dice": 2})
        print("Tool result:", tool_result)

        resource_result = await client.read_resource("data://config")
        print("Resource result:", resource_result)

        prompt_result = await client.get_prompt("analyze_data", {"data_points": [1, 2, 3]})
        print("Prompt result:", prompt_result)


def print_tools(tools):
    for tool in tools:
        print(f"Tool: {tool.name}")
        print(f"Description: {tool.description}")
        if tool.inputSchema:
            print(f"Parameters: {tool.inputSchema}")
        if hasattr(tool, "_meta") and tool._meta:
            fastmcp_meta = tool._meta.get("_fastmcp", {})
            print(f"Tags: {fastmcp_meta.get('tags', [])}")
        print("-" * 40 + "\n")


def print_resources(resources):
    for resource in resources:
        print(f"Resource URI: {resource.uri}")
        print(f"Name: {resource.name}")
        print(f"Description: {resource.description}")
        print(f"MIME Type: {resource.mimeType}")
        if hasattr(resource, "_meta") and resource._meta:
            fastmcp_meta = resource._meta.get("_fastmcp", {})
            print(f"Tags: {fastmcp_meta.get('tags', [])}")
        print("-" * 40 + "\n")


def print_prompts(prompts):
    for prompt in prompts:
        print(f"Prompt: {prompt.name}")
        print(f"Description: {prompt.description}")
        if prompt.arguments:
            print(f"Arguments: {[arg.name for arg in prompt.arguments]}")
        if hasattr(prompt, "_meta") and prompt._meta:
            fastmcp_meta = prompt._meta.get("_fastmcp", {})
            print(f"Tags: {fastmcp_meta.get('tags', [])}")
        print("-" * 40 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
