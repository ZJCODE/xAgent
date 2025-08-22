"""
Advanced example showing how to create specialized agents for AgentHTTPServer.
This demonstrates various configuration patterns and use cases.
"""

import asyncio
from xagent.core.agent import Agent
from xagent.interfaces.server import AgentHTTPServer
from xagent.components import MessageStorageLocal, MessageStorageRedis
from xagent.tools import web_search, draw_image
from xagent.utils import function_tool


# Custom tool example
@function_tool
def calculate_math(expression: str) -> str:
    """
    Calculate a mathematical expression safely.
    
    Args:
        expression: Mathematical expression to evaluate (e.g., "2 + 3 * 4")
        
    Returns:
        Result of the calculation as a string
    """
    try:
        # Safe evaluation for basic math
        allowed_chars = set("0123456789+-*/.() ")
        if not all(c in allowed_chars for c in expression):
            return "Error: Invalid characters in expression"
        
        result = eval(expression)
        return f"Result: {result}"
    except Exception as e:
        return f"Error: {str(e)}"


def create_research_agent():
    """Create a specialized research agent with web search capabilities."""
    return Agent(
        name="ResearchAgent",
        system_prompt=(
            "You are a research specialist. Use web search to find accurate, "
            "current information. Always cite your sources and provide multiple "
            "perspectives when possible."
        ),
        model="gpt-4o-mini",
        tools=[web_search],
        message_storage=MessageStorageLocal()
    )


def create_creative_agent():
    """Create a creative agent with image generation capabilities."""
    return Agent(
        name="CreativeAgent", 
        system_prompt=(
            "You are a creative assistant specialized in visual content creation. "
            "Help users generate images, provide design advice, and create "
            "compelling visual narratives."
        ),
        model="gpt-4o-mini",
        tools=[draw_image],
        message_storage=MessageStorageLocal()
    )


def create_math_agent():
    """Create a math-focused agent with calculation capabilities."""
    return Agent(
        name="MathAgent",
        system_prompt=(
            "You are a mathematics expert. Help users solve mathematical problems, "
            "explain concepts clearly, and perform calculations accurately."
        ),
        model="gpt-4o-mini", 
        tools=[calculate_math],
        message_storage=MessageStorageLocal()
    )


def create_general_agent():
    """Create a general-purpose agent with all available tools."""
    return Agent(
        name="GeneralAgent",
        system_prompt=(
            "You are a versatile AI assistant with access to web search, "
            "image generation, and mathematical calculation tools. "
            "Use the appropriate tools based on user requests."
        ),
        model="gpt-4o-mini",
        tools=[web_search, draw_image, calculate_math],
        message_storage=MessageStorageLocal()
    )


def run_agent_server(agent_type: str, port: int = 8010):
    """
    Run an HTTP server with a specific agent type.
    
    Args:
        agent_type: Type of agent to create ('research', 'creative', 'math', 'general')
        port: Port to run the server on
    """
    
    # Agent creation mapping
    agent_creators = {
        'research': create_research_agent,
        'creative': create_creative_agent, 
        'math': create_math_agent,
        'general': create_general_agent
    }
    
    if agent_type not in agent_creators:
        print(f"Unknown agent type: {agent_type}")
        print(f"Available types: {list(agent_creators.keys())}")
        return
    
    # Create the specified agent
    agent = agent_creators[agent_type]()
    
    # Create server with the agent
    server = AgentHTTPServer(agent=agent)
    
    print(f"Starting {agent.name} on http://localhost:{port}")
    print(f"Agent specialization: {agent_type}")
    print(f"Available tools: {list(agent.tools.keys())}")
    print("Press Ctrl+C to stop the server")
    
    try:
        server.run(host="localhost", port=port)
    except KeyboardInterrupt:
        print(f"\n{agent.name} server stopped.")


def main():
    """Main function to demonstrate different agent configurations."""
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python http_server_advanced_example.py <agent_type> [port]")
        print("Agent types: research, creative, math, general")
        print("Example: python http_server_advanced_example.py research 8010")
        return
    
    agent_type = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8010
    
    run_agent_server(agent_type, port)


if __name__ == "__main__":
    main()
