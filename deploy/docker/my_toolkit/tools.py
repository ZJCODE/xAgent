import asyncio
from xagent.utils.tool_decorator import function_tool

@function_tool()
def calculate_square(n: int) -> int:
    """Calculate the square of a number."""
    return n * n

@function_tool()
async def fetch_weather(city: str) -> str:
    """Fetch weather data from an API."""
    # Simulate API call
    await asyncio.sleep(0.5)
    return f"Weather in {city}: 22°C, Sunny"