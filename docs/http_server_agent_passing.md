# AgentHTTPServer with Custom Agent

The AgentHTTPServer now supports direct Agent instance passing, providing more flexibility in agent configuration.

## Usage Examples

### Traditional Config-Based Approach

```python
from xagent.interfaces import AgentHTTPServer

# Create server with config file
server = AgentHTTPServer(config_path="agent.yaml")
server.run()
```

### Direct Agent Approach

```python
from xagent.core import Agent
from xagent.interfaces import AgentHTTPServer
from xagent.tools import web_search, draw_image

# Create custom agent
agent = Agent(
    name="MyCustomAgent",
    system_prompt="You are a specialized assistant.",
    model="gpt-4o-mini",
    tools=[web_search, draw_image]
)

# Create server with agent
server = AgentHTTPServer(agent=agent)

# Start server
server.run()
```

### Advanced Example with Different Agent Types

```python
from xagent.core import Agent
from xagent.interfaces import AgentHTTPServer
from xagent.tools import web_search, draw_image
from xagent.components import MessageStorageRedis

def create_research_agent():
    """Create specialized research agent."""
    return Agent(
        name="ResearchAgent",
        system_prompt="You are a research specialist.",
        model="gpt-4o-mini",
        tools=[web_search],
        message_storage=MessageStorageRedis()
    )

def create_creative_agent():
    """Create specialized creative agent."""
    return Agent(
        name="CreativeAgent", 
        system_prompt="You are a creative visual assistant.",
        model="gpt-4o-mini",
        tools=[draw_image]
    )

# Start research agent server on port 8010
research_agent = create_research_agent()
research_server = AgentHTTPServer(agent=research_agent)
# research_server.run(host="localhost", port=8010)

# Start creative agent server on port 8011
creative_agent = create_creative_agent()
creative_server = AgentHTTPServer(agent=creative_agent)
# creative_server.run(host="localhost", port=8011)
```

## Benefits

1. **Programmatic Control**: Full control over agent configuration in code
2. **Multiple Agents**: Easy to run multiple specialized agents on different ports
3. **Custom Tools**: Directly register custom tools without config files
4. **Dynamic Configuration**: Modify agent behavior at runtime
5. **Testing**: Easier to create test agents with specific configurations

## Compatibility

The new agent parameter is fully backward compatible. Existing code using config files will continue to work unchanged.
