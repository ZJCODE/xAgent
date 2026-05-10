# xAgent

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

xAgent is a single-agent runtime with three entry points: Python API, CLI, and HTTP server.

- Chat via CLI, Python API, or HTTP
- One continuous agent-level message stream
- Speaker-aware messages via `user_id`
- Built-in Web UI and streaming responses
- Tool calling and image input
- Long-term memory enabled by default with local storage

## Quick Start

### Install

```bash
pip install myxagent
```

### Create a config

```bash
xagent init
```

The init wizard asks for the provider, model, API key, and identity text before writing files. Submit an empty identity to edit `identity.md` later.

Generated `config.yaml` example for OpenAI:

```yaml
provider:
  base_url: "https://api.openai.com/v1"
  api_key: "your_api_key_here"
  model: "gpt-5.4-mini"
```

Generated `identity.md` stores the agent's role and response style. You can enter it during init or leave it as a placeholder and edit it later.

### Start the CLI

```bash
xagent chat
```

Single-shot mode:

```bash
xagent chat "Hello"
```

## Message Model

Every chat appends to the agent's continuous message stream.

- `user_id` identifies the current speaker
- recent context is pulled from the agent's global recent-message window
- single-user and multi-user interactions use the same runtime model

## HTTP API

Start the server:

```bash
xagent server
```

Open the Web UI automatically:

```bash
xagent server --open
```

Configure host and port at startup:

```bash
xagent server --host 127.0.0.1 --port 8010
```

Send a chat message:

```bash
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "alice",
    "user_message": "Remember that my favorite city is Hangzhou."
  }'
```

Another speaker in the same conversation:

```bash
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "bob",
    "user_message": "Please summarize Alice'"'"'s plan and list the top risks."
  }'
```

Clear the message stream:

```bash
curl -X POST "http://localhost:8010/clear_messages"
```

Image input:

```bash
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user123",
    "user_message": "Describe this image.",
    "image_source": "https://example.com/image.jpg"
  }'
```

## Python API

```python
import asyncio
from xagent.core import Agent


async def main():
    agent = Agent(model="gpt-5.4-mini")

    reply = await agent.chat(
        user_message="Hello",
        user_id="alice",
    )
    print(reply)

    follow_up = await agent.chat(
        user_message="Summarize what the conversation has agreed on so far.",
        user_id="bob",
    )
    print(follow_up)


asyncio.run(main())
```

`image_source` accepts a single value or a list of values. Each item can be an image URL, local file path, or base64 data URI.

## Configure with `config.yaml`

Generate a starter config:

```bash
xagent init
```

The init wizard currently supports OpenAI, DeepSeek, Qwen, and custom OpenAI-compatible providers.

Example OpenAI config:

```yaml
provider:
  base_url: "https://api.openai.com/v1"
  api_key: "your_api_key_here"
  model: "gpt-5.4-mini"
```

`identity.md` stores the agent's role, personality, and behavior instructions. You can enter custom identity text during `xagent init` or submit an empty value to edit it later.

Add a starter structured-output schema during initialization:

```bash
xagent init --schema
```

`run_command` is built in by default and does not need YAML configuration.

Use a different xAgent directory:

```bash
xagent chat --dir ./my-agent
xagent server --dir ./my-agent --host 0.0.0.0 --port 8010 --open
```

## Documentation

- [Documentation Index](docs/README.md)
- [Getting Started](docs/getting_started.md)
- [Configuration Reference](docs/configuration_reference.md)
- [API Reference](docs/api_reference.md)
- [Memory System](docs/memory.md)
- [Message Storage Inheritance](docs/message_storage_inheritance.md)
- [Examples](examples/README.md)

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
