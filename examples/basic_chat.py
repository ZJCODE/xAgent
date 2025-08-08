"""
Basic Async Chat Example

This example demonstrates the basic usage of xAgent with async chat functionality.
"""

import asyncio
from xagent.core import Agent, Session
from xagent.tools import web_search

async def main():
    # Create agent with async-aware architecture
    agent = Agent(
        name="my_assistant",
        system_prompt="You are a helpful AI assistant.",
        model="gpt-4.1-mini",
        tools=[web_search]  # Add web search tool
    )

    # Create session for conversation management
    session = Session(
        session_id="session456",
    )

    # Async chat interaction
    response = await agent.chat("Hello, how are you?", session)
    print(response)

    # Continue conversation with context
    response = await agent.chat("What's the weather like in Hangzhou?", session)
    print(response)

# Run the async function
if __name__ == "__main__":
    asyncio.run(main())
