"""
Custom Tools Example (Sync and Async)

This example demonstrates how to create custom tools that can be either
synchronous or asynchronous, and how xAgent handles them automatically.
"""

import asyncio
import httpx
from xagent.utils.tool_decorator import function_tool
from xagent.core import Agent, Session

# Sync tools - automatically converted to async
@function_tool()
def calculate_square(n: int) -> int:
    """Calculate square of a number (CPU-intensive)."""
    import time
    time.sleep(0.1)  # Simulate CPU work
    return n * n

@function_tool()
def format_text(text: str, style: str) -> str:
    """Format text with various styles."""
    if style == "upper":
        return text.upper()
    elif style == "title":
        return text.title()
    return text

# Async tools - used directly for I/O operations
@function_tool()
async def fetch_weather(city: str) -> str:
    """Fetch weather data from API."""
    async with httpx.AsyncClient() as client:
        # Simulate weather API call
        await asyncio.sleep(0.5)
        return f"Weather in {city}: 22°C, Sunny"

async def main():
    # Mix of sync and async tools
    agent = Agent(
        tools=[calculate_square, format_text, fetch_weather],
        model="gpt-4.1-mini"
    )
    
    session = Session(user_id="user123")
    
    # Agent handles all tools automatically - sync tools run in thread pool
    response = await agent.chat(
        "Calculate the square of 15, format 'hello world' in title case, and get weather for Tokyo",
        session
    )
    print(response)

if __name__ == "__main__":
    asyncio.run(main())
