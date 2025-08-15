"""
Example demonstrating the new Agent usage without Session dependency.
The Agent now directly manages MessageStorageBase internally.
"""

import asyncio
from xagent.core.agent import Agent
from xagent.db import MessageStorageLocal, MessageStorageRedis


async def main():
    """Demonstrate the new Agent usage patterns."""
    
    # Example 1: Agent with default local message storage
    print("=== Example 1: Agent with default storage ===")
    agent1 = Agent(
        name="default_agent",
        system_prompt="You are a helpful assistant."
    )
    
    # Chat using user_id and session_id directly
    response1 = await agent1.chat(
        user_message="Hello, what can you help me with?",
        user_id="user_123",
        session_id="session_456"
    )
    print(f"Response 1: {response1}")
    
    # Continue the conversation with the same user_id and session_id
    response2 = await agent1.chat(
        user_message="Tell me a joke",
        user_id="user_123", 
        session_id="session_456"
    )
    print(f"Response 2: {response2}")
    
    print("\n" + "="*50 + "\n")
    
    # Example 2: Agent with explicit local message storage
    print("=== Example 2: Agent with explicit local storage ===")
    local_storage = MessageStorageLocal()
    agent2 = Agent(
        name="custom_agent",
        system_prompt="You are a specialized assistant.",
        message_storage=local_storage
    )
    
    response3 = await agent2.chat(
        user_message="What's the weather like?",
        user_id="user_789",
        session_id="session_abc"
    )
    print(f"Response 3: {response3}")
    
    print("\n" + "="*50 + "\n")
    
    # Example 3: Agent with Redis message storage (commented out as it requires Redis)
    print("=== Example 3: Agent with Redis storage (demo) ===")
    # Uncomment the following lines if you have Redis running:
    
    # redis_storage = MessageStorageRedis(
    #     host='localhost',
    #     port=6379,
    #     db=0
    # )
    # agent3 = Agent(
    #     name="redis_agent",
    #     system_prompt="You are a persistent assistant.",
    #     message_storage=redis_storage
    # )
    # 
    # response4 = await agent3.chat(
    #     user_message="Remember this: my favorite color is blue",
    #     user_id="user_persistent",
    #     session_id="session_persistent"
    # )
    # print(f"Response 4: {response4}")
    
    print("Redis storage example is commented out (requires Redis server)")
    
    print("\n" + "="*50 + "\n")
    
    # Example 4: Different users and sessions
    print("=== Example 4: Multiple users and sessions ===")
    
    # Same agent, different users/sessions
    await agent1.chat(
        user_message="Hello from user A",
        user_id="user_a",
        session_id="session_1"
    )
    
    await agent1.chat(
        user_message="Hello from user B",
        user_id="user_b", 
        session_id="session_1"
    )
    
    # Same user, different sessions
    await agent1.chat(
        user_message="This is session 2",
        user_id="user_a",
        session_id="session_2"
    )
    
    print("Multiple conversations handled independently")
    
    print("\n" + "="*50 + "\n")
    
    # Example 5: Using the __call__ method (shorthand)
    print("=== Example 5: Using __call__ method ===")
    
    response5 = await agent1(
        user_message="This uses the __call__ method",
        user_id="user_call",
        session_id="session_call"
    )
    print(f"Response 5: {response5}")


if __name__ == "__main__":
    asyncio.run(main())
