"""
Structured Output with Pydantic Example

This example demonstrates how to use xAgent to generate structured output
using Pydantic models for type-safe responses.
"""

import asyncio
from pydantic import BaseModel
from xagent.core import Agent
from xagent.tools import web_search

class WeatherReport(BaseModel):
    location: str
    temperature: int
    condition: str
    humidity: int

class Step(BaseModel):
    explanation: str
    output: str

class MathReasoning(BaseModel):
    steps: list[Step]
    final_answer: str


async def get_structured_response():
    
    agent = Agent(model="gpt-4.1-mini", 
                  tools=[web_search], 
                  output_type=WeatherReport) # You can set a default output type here or leave it None
    
    # Request structured output for weather
    weather_data = await agent.chat(
        user_message="what's the weather like in Hangzhou?",
        user_id="user123",
        session_id="session456"
    )
    
    print(f"Location: {weather_data.location}")
    print(f"Temperature: {weather_data.temperature}°F")
    print(f"Condition: {weather_data.condition}")
    print(f"Humidity: {weather_data.humidity}%")


    # Request structured output for mathematical reasoning (overrides output_type)
    reply = await agent.chat(
        user_message="how can I solve 8x + 7 = -23",
        user_id="user123", 
        session_id="session456",
        output_type=MathReasoning
    ) # Override output_type for this call
    for index, step in enumerate(reply.steps):
        print(f"Step {index + 1}: {step.explanation} => Output: {step.output}")
    print("Final Answer:", reply.final_answer)

if __name__ == "__main__":
    asyncio.run(get_structured_response())
