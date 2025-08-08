"""
Async Best Practices Examples

This file contains multiple examples demonstrating async best practices
when working with xAgent.
"""

import asyncio
import time
import httpx
from xagent.core import Agent, Session
from xagent.utils.tool_decorator import function_tool

# Example 1: Always Use Async Context
async def correct_async_usage():
    """✅ Correct: Run in async context"""
    agent = Agent()
    session = Session(user_id="user123")
    response = await agent.chat("Hello", session)
    print(response)

# Example 2: Flexible Tool Development (Sync or Async)
@function_tool()
def cpu_intensive_task(data: str) -> str:
    """Sync function for CPU-bound work - runs in thread pool"""
    import time
    time.sleep(1)  # Simulate CPU work
    return f"Processed: {data}"

@function_tool()
async def io_intensive_task(url: str) -> str:
    """Async function for I/O-bound work - runs directly"""
    import httpx
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        return response.text[:100]

async def flexible_tools_example():
    """Agent automatically handles both types concurrently"""
    agent = Agent(tools=[cpu_intensive_task, io_intensive_task])
    session = Session(user_id="user123")
    
    response = await agent.chat("Process some data and fetch a webpage", session)
    print(response)

# Example 3: Session Management
async def conversation_example():
    """✅ Reuse session for conversation continuity"""
    agent = Agent()
    session = Session(user_id="user123", session_id="chat001")
    
    # First message
    await agent.chat("My name is Alice", session)
    
    # Context is preserved automatically
    response = await agent.chat("What's my name?", session)
    print(response)  # Response: "Your name is Alice"

# Example 4: Error Handling in Async Context
async def robust_chat():
    """Proper error handling in async context"""
    agent = Agent()
    session = Session(user_id="user123")
    
    try:
        response = await agent.chat("Complex query", session)
        print(response)
    except Exception as e:
        print(f"Chat failed: {e}")
        # Handle gracefully

# Example 5: Memory Management for Long Conversations
async def long_conversation():
    """Control message history to prevent memory issues"""
    agent = Agent()
    session = Session(user_id="user123")
    
    # Control message history to prevent memory issues
    response = await agent.chat(
        "Tell me about AI", 
        session,
        history_count=10  # Only use last 10 messages for context
    )
    print(response)

# Example 6: Concurrent Tool Execution Demo
@function_tool()
def cpu_heavy_task(n: int) -> int:
    """Calculate sum of squares (CPU-intensive)."""
    time.sleep(0.1)  # Simulate heavy computation
    return sum(i**2 for i in range(n))

@function_tool()
async def network_request(url: str) -> str:
    """Fetch data from URL (I/O-intensive)."""
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        return response.text[:100]

@function_tool()
def simple_math(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b  # No async needed for simple operations

async def demo_concurrent_tools():
    """Concurrent execution example"""
    agent = Agent(tools=[
        cpu_heavy_task,    # Runs in thread pool
        network_request,   # Runs on event loop  
        simple_math        # Runs in thread pool
    ])
    
    session = Session(user_id="demo")
    
    # All tools execute concurrently when called by agent
    # - sync tools don't block the event loop
    # - async tools run directly for optimal I/O performance
    # - total execution time = max(individual_times), not sum
    
    response = await agent.chat(
        "Calculate sum of squares for 1000, fetch https://httpbin.org/json, and add 5+3",
        session
    )
    print(response)

async def main():
    """Run all examples"""
    print("1. Correct async usage:")
    await correct_async_usage()
    
    print("\n2. Flexible tools example:")
    await flexible_tools_example()
    
    print("\n3. Conversation example:")
    await conversation_example()
    
    print("\n4. Robust chat example:")
    await robust_chat()
    
    print("\n5. Long conversation example:")
    await long_conversation()
    
    print("\n6. Concurrent tools demo:")
    await demo_concurrent_tools()

if __name__ == "__main__":
    asyncio.run(main())
