# Configuration Reference

xAgent supports a directory-based `config.yaml` for CLI and HTTP server startup.

## Minimal Config

```yaml
agent:
  name: "MyAgent"
  system_prompt: "You are a helpful assistant."
  provider:
    model: "gpt-5.4-mini"
    api_key: "your_api_key_here"
```

By default xAgent reads `~/.xagent/config.yaml`. Use `--dir` to select another directory.

## Agent Section

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | `"Agent"` | Agent identifier |
| `system_prompt` | string | `"You are a helpful assistant."` | Base system prompt |
| `provider` | object | `{ model: "gpt-5.4-mini" }` | OpenAI-compatible provider config |
| `output_schema` | object | `null` | Structured output schema |

There is no `conversation_mode` config. All chats use the same continuous message-stream model.

## Message Model

- `user_id` identifies the current speaker
- recent context comes from the agent's single message stream
- single-user and multi-user interactions use the same runtime path

## Built-in Tools

`run_command` is enabled by default. It is not configured in YAML.

Register custom tools programmatically with the Python API: `Agent(tools=[...])`.

## Provider

Set `agent.provider` for OpenAI-compatible providers:

```yaml
agent:
  provider:
    model: "deepseek-v4-pro"
    base_url: "https://api.deepseek.com"
    api_key: "your_deepseek_api_key"
```

MiniMax example:

```yaml
agent:
  provider:
    model: "MiniMax-M2.7"
    base_url: "https://api.minimax.io/v1"
    api_key: "your_minimax_api_key"
```

## HTTP Server Runtime Flags

Host and port are runtime settings, not YAML settings:

```bash
xagent-server --host 127.0.0.1 --port 8010
```

## Storage Layout

By default the built-in runtime stores data locally under the selected xAgent directory.

```bash
xagent --dir ./my-project-agent
```

Local mode stores:

- message history in SQLite
- diary memory in markdown files under `<agent_name>_memory/`

Workspace layout:

```text
<xagent-dir>/
  config.yaml
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

  provider:
    model: "gpt-5.4-mini"
    api_key: "your_api_key_here"
```
