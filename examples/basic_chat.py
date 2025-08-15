"""
Basic Async Chat Example

This example demonstrates the basic usage of xAgent with async chat functionality.
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

    # Async chat interaction with user_id and session_id
    response = await agent.chat(
        user_message="Hello, how are you?", 
        user_id="user123",
        session_id="session456"
    )
    print(response)

    # Continue conversation with context using the same user_id and session_id
    response = await agent.chat(
        user_message="What's the weather like in Hangzhou?", 
        user_id="user123",
        session_id="session456"
    )
    print(response)

# Run the async function
if __name__ == "__main__":
    asyncio.run(main())
