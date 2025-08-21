# API Reference

## Core Classes

### Agent Class

Main AI agent class for handling conversations and tool execution.

```python
Agent(
    name: Optional[str] = None,
    system_prompt: Optional[str] = None, 
    model: Optional[str] = None,
    client: Optional[AsyncOpenAI] = None,
    tools: Optional[list] = None,
    mcp_servers: Optional[str | list] = None,
    sub_agents: Optional[List[Union[tuple[str, str, str], 'Agent']]] = None,
    output_type: Optional[type[BaseModel]] = None,
    message_storage: Optional[MessageStorageBase] = None
)
```

#### Key Methods

- `async chat(user_message, user_id, session_id, **kwargs) -> str | BaseModel`: Main chat interface
- `async __call__(user_message, user_id, session_id, **kwargs) -> str | BaseModel`: Shorthand for chat
- `as_tool(name, description) -> Callable`: Convert agent to tool

#### Chat Method Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `user_message` | string | **required** | The user's message content |
| `user_id` | string | "default_user" | User identifier for message storage |
| `session_id` | string | "default_session" | Session identifier for message storage |
| `history_count` | integer | `16` | Number of previous messages to include in context |
| `max_iter` | integer | `10` | Maximum model call attempts for complex reasoning |
| `max_concurrent_tools` | integer | `10` | Maximum number of concurrent tool calls |
| `image_source` | string | optional | Image URL, file path, or base64 string for analysis |
| `output_type` | type | optional | Pydantic model for structured output |
| `stream` | boolean | `false` | Enable streaming response |

#### Agent Constructor Parameters

- `name`: Agent identifier (default: "default_agent")
- `system_prompt`: Instructions for the agent behavior
- `model`: OpenAI model to use (default: "gpt-4.1-mini")
- `client`: Custom AsyncOpenAI client instance
- `tools`: List of function tools
- `mcp_servers`: MCP server URLs for dynamic tool loading
- `sub_agents`: List of sub-agent configurations (name, description, server URL)
- `output_type`: Pydantic model for structured output
- `message_storage`: MessageStorageBase instance for conversation persistence

### HTTPAgentServer Class

HTTP server for agent interactions with REST API endpoints.

```python
HTTPAgentServer(
    config_path: Optional[str] = None,
    toolkit_path: Optional[str] = None,
    agent: Optional[Agent] = None
)
```

The HTTPAgentServer can be initialized in two ways:

1. **Traditional approach** using configuration files:
```python
server = HTTPAgentServer(config_path="config.yaml")
server.run()
```

2. **Direct agent approach** using a pre-configured Agent instance:
```python
agent = Agent(name="MyAgent", tools=[web_search])
server = HTTPAgentServer(agent=agent)
server.run()
```

#### Constructor Parameters

- `config_path`: Path to configuration file (ignored if agent is provided)
- `toolkit_path`: Path to toolkit directory (ignored if agent is provided)
- `agent`: Pre-configured Agent instance to use directly

#### API Endpoints

- `GET /health`: Health check endpoint
- `POST /chat`: Main chat interaction endpoint
- `POST /clear_session`: Clear conversation session

#### Chat Endpoint Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `user_id` | string | ✅ | - | Unique user identifier |
| `session_id` | string | ✅ | - | Conversation session identifier |
| `user_message` | string | ✅ | - | User's message content |
| `image_source` | string | ❌ | `null` | Image URL, file path, or base64 string |
| `stream` | boolean | ❌ | `false` | Enable Server-Sent Events streaming |
| `history_count` | integer | ❌ | `16` | Number of previous messages to include |
| `max_iter` | integer | ❌ | `10` | Maximum model call attempts |
| `max_concurrent_tools` | integer | ❌ | `10` | Maximum number of concurrent tool calls |

### Message Storage Classes

#### MessageStorageBase (Abstract)

Base interface for message storage implementations.

**Key Methods:**
- `async add_messages(user_id, session_id, messages) -> None`: Store messages
- `async get_messages(user_id, session_id, count) -> List[Message]`: Retrieve history
- `async clear_session(user_id, session_id) -> None`: Clear conversation history

#### MessageStorageLocal

In-memory message storage (default option).

```python
from xagent.components import MessageStorageLocal

storage = MessageStorageLocal()
agent = Agent(message_storage=storage)
```

#### MessageStorageRedis

Redis-based persistent message storage.

```python
from xagent.components import MessageStorageRedis

storage = MessageStorageRedis()
agent = Agent(message_storage=storage)
```

Requires `REDIS_URL` environment variable.

## Workflow Classes

### Workflow Class

Orchestrates multi-agent workflows with different execution patterns.

```python
from xagent.multi.workflow import Workflow

workflow = Workflow()
```

#### Methods

- `async run_sequential(agents, task) -> WorkflowResult`: Execute agents in sequence
- `async run_parallel(agents, task) -> WorkflowResult`: Execute agents in parallel
- `async run_graph(agents, dependencies, task) -> WorkflowResult`: Execute with complex dependencies
- `async run_hybrid(task, stages) -> dict`: Execute multi-stage workflows

## Utility Functions

### Tool Decorator

```python
from xagent.utils.tool_decorator import function_tool

@function_tool(
    name: Optional[str] = None,
    description: Optional[str] = None,
    param_descriptions: Optional[Dict[str, str]] = None
)
def your_function():
    pass
```

### DSL Utilities

```python
from xagent.multi.workflow import validate_dsl_syntax, parse_dependencies_dsl

# Validate DSL syntax
is_valid, error = validate_dsl_syntax("A->B, B->C")

# Parse DSL into dependencies
dependencies = parse_dependencies_dsl("A->B, B->C")
```

## Configuration Schema

### Agent Configuration (YAML)

```yaml
agent:
  name: "AgentName"
  system_prompt: "System prompt text"
  model: "gpt-4.1-mini"
  
  capabilities:
    tools:
      - "tool_name"
    mcp_servers:
      - "http://localhost:8001/mcp/"
  
  sub_agents:
    - name: "sub_agent_name"
      description: "Sub-agent description"
      server_url: "http://localhost:8011"
  
  output_schema:
    class_name: "ModelName"
    fields:
      field_name:
        type: "str"
        description: "Field description"
  
  message_storage: "local"  # or "redis"

server:
  host: "0.0.0.0"
  port: 8010
```

## Response Types

### Standard Response

```python
response: str = await agent.chat("Hello")
```

### Structured Response

```python
from pydantic import BaseModel

class MyModel(BaseModel):
    title: str
    content: str

response: MyModel = await agent.chat("Generate content", output_type=MyModel)
```

### Streaming Response

```python
response = await agent.chat("Tell me a story", stream=True)
async for chunk in response:
    print(chunk, end="")
```

## Error Handling

### Common Exceptions

- `ValidationError`: Invalid input parameters
- `OpenAIError`: API communication issues
- `ToolExecutionError`: Tool execution failures
- `MessageStorageError`: Storage operation failures

### Example Error Handling

```python
try:
    response = await agent.chat("Hello", user_id="user123", session_id="session456")
except ValidationError as e:
    print(f"Invalid input: {e}")
except Exception as e:
    print(f"Unexpected error: {e}")
```

## Best Practices

### Performance Optimization

- Use appropriate `history_count` based on context needs
- Set reasonable `max_iter` for task complexity
- Configure `max_concurrent_tools` based on system resources
- Use Redis storage for production deployments

### Security Considerations

- Validate all user inputs
- Use environment variables for sensitive configuration
- Implement proper authentication for HTTP endpoints
- Monitor and log all interactions

### Scaling Recommendations

- Use Redis for message storage in multi-instance deployments
- Implement load balancing for HTTP servers
- Monitor agent performance and resource usage
- Use appropriate model sizes for different tasks
