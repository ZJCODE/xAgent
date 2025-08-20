from collections import Counter
import asyncio
from xagent.utils.tool_decorator import function_tool

@function_tool()
def char_count(text: str) -> dict:
    """
    Count the frequency of each character in the given text.
    
    Args:
        text (str): The input text to analyze.
        
    Returns:
        dict: A dictionary with characters as keys and their counts as values.
    """
    if not text:
        return {}
    
    # Use Counter to count character frequencies
    return dict(Counter(text))

@function_tool()
async def fetch_weather(city: str) -> str:
    """Fetch weather data from an API."""
    # Simulate API call
    await asyncio.sleep(0.5)
    return f"Weather in {city}: 22°C, Sunny"