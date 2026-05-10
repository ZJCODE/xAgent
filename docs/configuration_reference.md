# Configuration Reference

xAgent uses a directory-based runtime layout. The selected directory contains:

- `config.yaml` for runtime configuration
- `identity.md` for agent role and behavior instructions
- local message and memory data created at runtime

By default xAgent reads `~/.xagent`. Use `--dir` on each command to select another directory.

## Init Wizard

Run:

```bash
xagent init
```

The wizard asks for the provider, model, API key, and identity text before writing `config.yaml` and `identity.md`. Submit an empty identity to edit the file later.

Provider choices:

| Provider | Base URL | Model choices |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.4-nano`, `gpt-5.5`, or decide later |
| DeepSeek | `https://api.deepseek.com` | `deepseek-v4-flash`, `deepseek-v4-pro`, or decide later |
| Qwen | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen3.6-plus`, `qwen3.6-flash`, `qwen3.6-max-preview`, or decide later |
| Custom | entered during init | placeholder model by default |

Choosing to decide later writes `your_model_here`. Leaving the API key blank writes `your_api_key_here`.

## Minimal Config

```yaml
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

`config.yaml` does not contain prompt text. `system_prompt` is not a supported config key.

## Config Fields

| Field | Type | Required | Description |
|---|---|---|---|
| `provider` | object | yes | OpenAI-compatible provider config |
| `output_schema` | object | no | Structured output schema |

There is no `conversation_mode` config. All chats use the same continuous message-stream model.

## Provider

Set `provider` for OpenAI-compatible providers:

```yaml
provider:
  base_url: "https://api.deepseek.com"
  api_key: "your_deepseek_api_key"
  model: "deepseek-v4-pro"
```

MiniMax example:

```yaml
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
- diary memory in markdown files under `memory/`

Workspace layout:

```text
<xagent-dir>/
  config.yaml
  identity.md
  messages.sqlite3
  memory/
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
