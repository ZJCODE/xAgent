#!/usr/bin/env python3
"""
Example showing how to create a weather tool that matches the OpenAI function specification
using the enhanced function_tool decorator.
"""

from typing import Literal
import sys
import os

# Add the parent directory to the path so we can import xagent
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from xagent.utils.tool_decorator import function_tool


@function_tool(
    name="get_weather",
    description="Retrieves current weather for the given location.",
    strict=True,
    param_descriptions={
        "location": "City and country e.g. Bogotá, Colombia",
        "units": "Units the temperature will be returned in."
    }
)
def get_weather(
    location: str,
    units: Literal["celsius", "fahrenheit"]
) -> str:
    """
    Retrieves current weather for the given location.
    
    Args:
        location: City and country e.g. Bogotá, Colombia
        units: Units the temperature will be returned in.
    
    Returns:
        Weather information as a string
    """
    # This is a mock implementation
    if units == "celsius":
        temp_unit = "°C"
        temp = "22"
    else:
        temp_unit = "°F"
        temp = "72"
    
    return f"Weather in {location}: {temp}{temp_unit}, partly cloudy"


if __name__ == "__main__":
    import json
    
    # Print the tool specification
    print("Generated tool specification:")
    print(json.dumps(get_weather.tool_spec, indent=2))
    
    # Test the function
    print("\nTesting the function:")
    import asyncio
    
    async def test():
        result = await get_weather("Bogotá, Colombia", "celsius")
        print(f"Result: {result}")
    
    asyncio.run(test())
