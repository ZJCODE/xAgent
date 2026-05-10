# Configuration Reference

xAgent uses a directory-based runtime layout. The selected directory contains:

- `config.yaml` for runtime configuration
- `identity.md` for agent role and behavior instructions
- local message and memory data created at runtime

By default xAgent reads `~/.xagent`. Use `--dir` on each command to select another directory.

## Minimal Config

```yaml
agent:
  name: "assistant"
  provider:
    base_url: "https://api.openai.com/v1"
    api_key: "your_api_key_here"
    model: "gpt-5.4-mini"
```

## Identity File

Put custom role, personality, and response-style instructions in `identity.md`:

```markdown
# Identity

You are a helpful assistant.
Answer clearly, keep responses practical, and adapt to the user's language.
```

`config.yaml` does not contain prompt text. `agent.system_prompt` is not a supported config key.

## Agent Section

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Runtime identifier used for storage and assistant message attribution |
| `provider` | object | yes | OpenAI-compatible provider config |
| `output_schema` | object | no | Structured output schema |

There is no `conversation_mode` config. All chats use the same continuous message-stream model.

## Provider

Set `agent.provider` for OpenAI-compatible providers:

```yaml
agent:
  name: "deepseek-agent"
  provider:
    base_url: "https://api.openai.com/v1"
    api_key: "your_deepseek_api_key"
    model: "deepseek-v4-pro"
```

MiniMax example:

```yaml
agent:
  name: "minimax-agent"
  provider:
    base_url: "https://api.minimax.io/v1"
    api_key: "your_minimax_api_key"
    model: "MiniMax-M2.7"
```

## Message Model

- `user_id` identifies the current speaker
- recent context comes from the agent's single message stream
- single-user and multi-user interactions use the same runtime path

## Built-in Tools

`run_command` is enabled by default. It is not configured in YAML.

Register custom tools programmatically with the Python API: `Agent(tools=[...])`.

## HTTP Server Runtime Flags

Host and port are command options, not YAML settings:

```bash
xagent server --host 127.0.0.1 --port 8010
```

## Storage Layout

By default the built-in runtime stores data locally under the selected xAgent directory.

```bash
xagent init --dir ./my-project-agent
xagent chat --dir ./my-project-agent
```

Local mode stores:

- message history in SQLite
- diary memory in markdown files under `<agent_name>_memory/`

Workspace layout:

```text
<xagent-dir>/
  config.yaml
  identity.md
  <agent_name>_messages.sqlite3
  <agent_name>_memory/
    daily/
    weekly/
    monthly/
    yearly/
```

## Structured Output Schema

Generate a starter schema during initialization:

```bash
xagent init --schema
```

Or define a Pydantic model directly in YAML:

```yaml
agent:
  name: "analysis"
  provider:
    base_url: "https://api.openai.com/v1"
    api_key: "your_api_key_here"
    model: "gpt-5.4-mini"
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
