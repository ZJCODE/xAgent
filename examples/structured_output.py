"""
Structured Output with Pydantic Example

This example demonstrates how to use xAgent to generate structured output
using Pydantic models for type-safe responses.
"""

import asyncio
from pydantic import BaseModel
from xagent.core import Agent, Session
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
    agent = Agent(model="gpt-4.1-mini", tools=[web_search])
    session = Session(user_id="user123")
    
    # Request structured output for weather
    weather_data = await agent.chat(
        "what's the weather like in Hangzhou?",
        session,
        output_type=WeatherReport
    )
    
    print(f"Location: {weather_data.location}")
    print(f"Temperature: {weather_data.temperature}°F")
    print(f"Condition: {weather_data.condition}")
    print(f"Humidity: {weather_data.humidity}%")

    # Request structured output for mathematical reasoning
    reply = await agent.chat("how can I solve 8x + 7 = -23", session, output_type=MathReasoning)
    for index, step in enumerate(reply.steps):
        print(f"Step {index + 1}: {step.explanation} => Output: {step.output}")
    print("Final Answer:", reply.final_answer)

if __name__ == "__main__":
    asyncio.run(get_structured_response())
