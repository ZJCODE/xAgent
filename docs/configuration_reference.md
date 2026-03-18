# Configuration Reference

xAgent supports YAML-based configuration for CLI and HTTP server startup.

## Minimal Config

```yaml
agent:
  name: "MyAgent"
  system_prompt: "You are a helpful assistant."
  model: "gpt-5-mini"

server:
  host: "0.0.0.0"
  port: 8010
```

## Agent Section

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | `"Agent"` | Agent identifier |
| `system_prompt` | string | `"You are a helpful assistant."` | Base system prompt |
| `model` | string | `"gpt-4o-mini"` | OpenAI model name |
| `storage_mode` | string | `"local"` | `local` or `cloud` |
| `workspace` | string | `"~/.xagent"` | Local storage root for SQLite and Chroma |
| `capabilities` | object | `{}` | Tool and MCP configuration |
| `output_schema` | object | `null` | Structured output schema |

There is no `conversation_mode` config. All chats use the same continuous message-stream model.

## Message Model

- `user_id` identifies the current speaker
- recent context comes from the agent's single message stream
- single-user and multi-user interactions use the same runtime path

## Capabilities

### Built-in Tools

```yaml
agent:
  capabilities:
    tools:
      - "web_search"
      - "draw_image"
      - "run_command"
```

Available built-in tools:

- `web_search`
- `draw_image`
- `run_command`

### MCP Servers

```yaml
agent:
  capabilities:
    mcp_servers:
      - "http://localhost:8001/mcp/"
      - "http://localhost:8002/mcp/"
```

### Custom Toolkit

Load custom tools at runtime:

```bash
xagent-server --config agent.yaml --toolkit_path my_toolkit/
```

## Storage Mode

### Local

```yaml
agent:
  storage_mode: "local"
  workspace: "./my_project_data"
```

Local mode stores:

- message history in SQLite
- long-term memory in ChromaDB

Workspace layout:

```text
<workspace>/
  <agent_name>_messages.sqlite3
  chroma/
```

### Cloud

```yaml
agent:
  storage_mode: "cloud"
```

Cloud mode requires:

```bash
export REDIS_URL=redis://localhost:6379/0
export UPSTASH_VECTOR_REST_URL=https://your-database.upstash.io
export UPSTASH_VECTOR_REST_TOKEN=your_token_here
```

## Structured Output Schema

Define a Pydantic model in YAML:

```yaml
agent:
  output_schema:
    class_name: "AnalysisResult"
    fields:
      summary:
        type: "str"
        description: "Short summary"
      action_items:
        type: "list"
        items: "str"
        description: "Recommended next steps"
      confidence:
        type: "float"
        description: "Confidence score"
```

Supported field types:

- `str`
- `int`
- `float`
- `bool`
- `list`

## Full Example

```yaml
agent:
  name: "Assistant"
  system_prompt: |
    You are a helpful assistant.
    Be concise and accurate.
  model: "gpt-5-mini"
  storage_mode: "local"
  workspace: "./data"

  capabilities:
    tools:
      - "web_search"
      - "run_command"
    mcp_servers: []

server:
  host: "0.0.0.0"
  port: 8010
```
