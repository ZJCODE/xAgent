#!/usr/bin/env python3
"""Example showing weather tool creation with function_tool decorator."""

import sys
import os
from typing import Literal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from xagent.utils.tool_decorator import function_tool


@function_tool(
    description="Retrieves current weather for the given location",
    param_descriptions={
        "location": "City and country e.g. Bogotá, Colombia",
        "units": "Temperature units"
    }
)
def get_weather(location: str, units: Literal["celsius", "fahrenheit"]) -> str:
    """Get weather for a location."""
    temp = "22°C" if units == "celsius" else "72°F"
    return f"Weather in {location}: {temp}, partly cloudy"


if __name__ == "__main__":
    import json
    import asyncio
    
    print("Tool spec:", json.dumps(get_weather.tool_spec, indent=2))
    
    async def test():
        result = await get_weather("Bogotá, Colombia", "celsius")
        print(f"Result: {result}")
    
    asyncio.run(test())
