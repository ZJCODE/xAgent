"""
Streaming Chat Example

This example demonstrates the streaming chat functionality of xAgent.
"""

import asyncio
from xagent.core import Agent
from xagent.tools import web_search

async def main():
    # Create agent with async-aware architecture
    agent = Agent(
        name="my_assistant",
        system_prompt="You are a helpful AI assistant.",
        model="gpt-4.1-mini",
        tools=[web_search]  # Add web search tool
    )

    # Async streaming chat interaction
    response = await agent.chat(
        user_message="Hello, how are you?", 
        user_id="user123",
        session_id="session456",
        stream=True
    )
    async for event in response:
        print(event, end="", flush=True)
    print()  # New line after streaming

    # Continue conversation with context using streaming
    response = await agent.chat(
        user_message="What's the weather like in Hangzhou?", 
        user_id="user123",
        session_id="session456",
        stream=True
    )
    async for event in response:
        print(event, end="", flush=True)
    print()  # New line after streaming

# Run the async function
if __name__ == "__main__":
    asyncio.run(main())
