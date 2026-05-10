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
from xagent.tools import run_command

# Create custom agent
agent = Agent(
    name="MyCustomAgent",
    system_prompt="You are a specialized assistant.",
    model="gpt-5.4-mini",
    tools=[run_command]
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
from xagent.tools import run_command

def create_ops_agent():
    """Create specialized local-ops agent."""
    return Agent(
        name="OpsAgent",
        system_prompt="You help inspect local project state safely.",
        model="gpt-5.4-mini",
        tools=[run_command],
        workspace="./data/ops_agent"
    )

def create_chat_agent():
    """Create specialized chat-only agent."""
    return Agent(
        name="ChatAgent",
        system_prompt="You are a concise assistant.",
        model="gpt-5.4-mini",
        tools=[]
    )

# Start ops agent server on port 8010
ops_agent = create_ops_agent()
ops_server = AgentHTTPServer(agent=ops_agent)
# ops_server.run(host="localhost", port=8010)

# Start chat agent server on port 8011
chat_agent = create_chat_agent()
chat_server = AgentHTTPServer(agent=chat_agent)
# chat_server.run(host="localhost", port=8011)
```

## Benefits

1. **Programmatic Control**: Full control over agent configuration in code
2. **Multiple Agents**: Easy to run multiple specialized agents on different ports
3. **Custom Tools**: Directly register custom tools without config files
4. **Dynamic Configuration**: Modify agent behavior at runtime
5. **Testing**: Easier to create test agents with specific configurations

## Compatibility

The `agent` parameter works alongside config-file startup. Built-in tools are provider-neutral; add search, image, or other provider-specific capabilities through a custom toolkit.
