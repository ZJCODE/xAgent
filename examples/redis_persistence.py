"""
Advanced Chat with Redis Persistence Example

This example demonstrates how to use xAgent with Redis persistence for maintaining
conversation history across sessions.
"""

import asyncio
from xagent.core import Agent
from xagent.db import MessageStorageRedis

async def chat_with_persistence():
    # Initialize Redis-backed message storage
    message_storage = MessageStorageRedis()
    
    # Create agent with Redis persistence
    agent = Agent(
        name="persistent_agent",
        model="gpt-4.1-mini",
        tools=[],
        message_storage=message_storage
    )

    # Chat with automatic message persistence
    response = await agent.chat(
        user_message="Remember this: my favorite color is blue",
        user_id="user123",
        session_id="persistent_session"
    )
    print(response)
    
    # Later conversation - context is preserved in Redis
    response = await agent.chat(
        user_message="What's my favorite color?",
        user_id="user123",
        session_id="persistent_session"
    )
    print(response)

if __name__ == "__main__":
    asyncio.run(chat_with_persistence())
