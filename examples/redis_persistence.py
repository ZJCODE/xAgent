"""
Advanced Chat with Redis Persistence Example

This example demonstrates how to use xAgent with Redis persistence for maintaining
conversation history across sessions.
"""

import asyncio
from xagent.core import Agent, Session
from xagent.db import MessageDB

async def chat_with_persistence():
    # Initialize Redis-backed message storage
    message_db = MessageDB()
    
    # Create agent
    agent = Agent(
        name="persistent_agent",
        model="gpt-4.1-mini",
        tools=[]
    )

    # Create session with Redis persistence
    session = Session(
        user_id="user123", 
        session_id="persistent_session",
        message_db=message_db
    )

    # Chat with automatic message persistence
    response = await agent.chat("Remember this: my favorite color is blue", session)
    print(response)
    
    # Later conversation - context is preserved in Redis
    response = await agent.chat("What's my favorite color?", session)
    print(response)

if __name__ == "__main__":
    asyncio.run(chat_with_persistence())
