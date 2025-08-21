"""
Example demonstrating HTTPAgentServer with a custom pre-configured Agent.
This shows how to create an Agent with specific settings and pass it directly to HTTPAgentServer.
"""

import asyncio
from xagent.core.agent import Agent
from xagent.interfaces.server import HTTPAgentServer
from xagent.components import MessageStorageLocal
from xagent.tools import web_search, draw_image


def create_custom_agent():
    """Create a custom agent with specific configuration."""
    
    # Create custom message storage
    message_storage = MessageStorageLocal()
    
    # Create agent with custom settings
    agent = Agent(
        name="CustomWebAgent", 
        system_prompt=(
            "You are a specialized web research assistant. "
            "You excel at finding accurate information and creating visual content. "
            "Always provide source citations when using web search results."
        ),
        model="gpt-4o-mini",
        tools=[web_search, draw_image],  # Custom tool selection
        message_storage=message_storage
    )
    
    return agent


def main():
    """Demonstrate creating HTTPAgentServer with a custom agent."""
    
    # Create custom agent
    custom_agent = create_custom_agent()
    
    # Create server with custom agent
    print("Creating HTTPAgentServer with custom agent...")
    server = HTTPAgentServer(agent=custom_agent)
    
    print(f"Server created successfully!")
    print(f"Agent name: {server.agent.name}")
    print(f"Agent model: {server.agent.model}")
    print(f"Available tools: {len(server.agent.tools)}")
    print(f"Tool names: {list(server.agent.tools.keys())}")
    
    # Start the server
    print("Starting server on http://localhost:8010")
    print("You can now send requests to the server with your custom agent!")
    print("Press Ctrl+C to stop the server")
    
    try:
        server.run(host="localhost", port=8010)
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
