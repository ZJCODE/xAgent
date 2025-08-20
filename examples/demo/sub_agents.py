"""
Agent as Tool Pattern Example

This example demonstrates how to use agents as tools, creating specialized
agents that can be composed together for complex tasks.
"""

import asyncio
from xagent.core import Agent
from xagent.tools import web_search

async def agent_as_tool_example():
    # Create specialized agents
    researcher_agent = Agent(
        name="research_specialist",
        system_prompt="You are a research expert. Gather information by using web search, analyze data, and provide well-researched insights.",
        description="Research topics and provide detailed analysis",
        model="gpt-4.1-mini",
        tools=[web_search]  # Add web search tool for research purposes
    )
    
    writing_agent = Agent(
        name="writing_specialist", 
        system_prompt="You are a professional writer. Create engaging content.",
        description="Write and edit content",
        model="gpt-4.1-mini"
    )
    
    # Main coordinator agent with specialist tools
    coordinator = Agent(
        name="coordinator",
        sub_agents=[researcher_agent, writing_agent],
        system_prompt="You are a coordination agent that breaks down complex tasks and delegates them to specialist agents. Analyze requests, create execution plans, delegate to the right specialist (research_specialist for information gathering, writing_specialist for writing), and synthesize results into coherent outputs. Solve tasks step-by-step, ensuring clarity and thoroughness in your responses.",
        model="gpt-4.1"
    )
    
    # Complex multi-step task
    response = await coordinator.chat(
        user_message="Research the latest advancements in AI technology and write a brief summary",
        user_id="user123",
        session_id="session456"
    )
    print(response)

if __name__ == "__main__":
    asyncio.run(agent_as_tool_example())
