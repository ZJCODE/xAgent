# Configuration Reference

xAgent supports YAML-based configuration for CLI and HTTP server startup.

## Minimal Config

```yaml
agent:
  name: "MyAgent"
  system_prompt: "You are a helpful assistant."
  model: "gpt-5.4-mini"

server:
  host: "0.0.0.0"
  port: 8010
```

## Agent Section

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | `"Agent"` | Agent identifier |
| `system_prompt` | string | `"You are a helpful assistant."` | Base system prompt |
| `model` | string | `"gpt-5.4-mini"` | OpenAI-compatible chat model name |
| `provider` | object | `null` | Optional OpenAI-compatible provider client config |
| `workspace` | string | `"~/.xagent"` | Local storage root for the shared SQLite database |
| `capabilities` | object | `{}` | Tool configuration |
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
      - "run_command"
```

Available built-in tools:

- `run_command`

### Custom Toolkit

Load custom tools at runtime:

```bash
xagent-server --config agent.yaml --toolkit_path my_toolkit/
```

## Provider

By default xAgent uses the OpenAI SDK defaults, including `OPENAI_API_KEY`.
Set `agent.provider` for OpenAI-compatible providers:

```yaml
agent:
  model: "deepseek-v4-pro"
  provider:
    base_url: "https://api.deepseek.com"
    api_key_env: "DEEPSEEK_API_KEY"
```

MiniMax example:

```yaml
agent:
  model: "MiniMax-M2.7"
  provider:
    base_url: "https://api.minimax.io/v1"
    api_key_env: "MINIMAX_API_KEY"
```

## Storage Layout

By default the built-in runtime stores data locally:

```yaml
agent:
  workspace: "./my_project_data"
```

Local mode stores:

- message history in SQLite
- diary memory in markdown files under `<agent_name>_memory/`

Workspace layout:

```text
<workspace>/
  <agent_name>_messages.sqlite3
  <agent_name>_memory/
    daily/
    weekly/
    monthly/
    yearly/
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
  model: "gpt-5.4-mini"
  workspace: "./data"

  capabilities:
    tools:
      - "run_command"

server:
  host: "0.0.0.0"
  port: 8010
```
