import asyncio
from xagent.core import Agent, Session
from xagent.db import MessageDB

async def main():
    # Create agent with async-aware architecture
    agent = Agent(
        name="my_assistant",
        system_prompt="You are a helpful AI assistant.",
        model="gpt-4.1-mini"  # Using latest model
    )

    # Create session for conversation management
    session = Session(
        session_id="session456",
    )

    # Async chat interaction
    response = await agent.chat("Hello, how are you?", session)
    print(response)

    # Continue conversation with context
    response = await agent.chat("What's the weather like?", session)
    print(response)

# Run the async function
asyncio.run(main())