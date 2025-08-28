import asyncio
from xagent.core import Agent

async def main():
    # Create agent with memory enabled
    agent = Agent(
        name="memory_assistant",
        system_prompt="You are a helpful assistant with long-term memory."
    )
    
    # Chat with memory enabled
    response = await agent.chat(
        user_message="Hi, I'm John. I work as a software engineer at Google and live in San Francisco.",
        user_id="john_12345",
        session_id="session_1",
        enable_memory=True  # Enable memory for this conversation
    )
    print(response)
    
    # In a later conversation, the agent will remember John's details
    response = await agent.chat(
        user_message="What do you know about me?",
        user_id="john_12345", 
        session_id="session_2",
        enable_memory=True
    )
    print(response)  # Agent will recall John's job and location

asyncio.run(main())