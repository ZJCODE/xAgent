"""
Agent as Tool Pattern Example

This example demonstrates how to use agents as tools, creating specialized
agents that can be composed together for complex tasks.
"""

import asyncio
from xagent.core import Agent
from xagent.components import MessageStorageRedis
from xagent.tools import web_search

async def agent_as_tool_example():
    # Create specialized agents with Redis message storage
    message_storage = MessageStorageRedis()
    
    researcher_agent = Agent(
        name="research_specialist",
        system_prompt="You are a research expert. Gather information, analyze data, and provide well-researched insights.",
        model="gpt-4.1-mini",
        tools=[web_search],  # Add web search tool for research purposes
        message_storage=message_storage
    )
    
    writing_agent = Agent(
        name="writing_specialist", 
        system_prompt="You are a professional writer. Create engaging content.",
        model="gpt-4.1-mini",
        message_storage=message_storage
    )
    
    # Convert agents to tools
    research_tool = researcher_agent.as_tool(
        name="researcher",
        description="Research topics and provide detailed analysis"
    )
    
    writing_tool = writing_agent.as_tool(
        name="content_writer",
        description="Write and edit content"
    )
    
    # Main coordinator agent with specialist tools
    coordinator = Agent(
        name="coordinator",
        tools=[research_tool, writing_tool],
        system_prompt="You are a coordination agent that breaks down complex tasks and delegates them to specialist agents. Analyze requests, create execution plans, delegate to the right specialist (researcher for information gathering, content_writer for writing), and synthesize results into coherent outputs.",
        model="gpt-4.1",
        message_storage=message_storage
    )
    
    # Complex multi-step task
    response = await coordinator.chat(
        user_message="Research the benefits of renewable energy and write a brief summary",
        user_id="user123",
        session_id="session456"
    )
    print(response)

if __name__ == "__main__":
    asyncio.run(agent_as_tool_example())
