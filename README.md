# xAgent

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

xAgent is a single-agent runtime with three entry points: Python API, CLI, and HTTP server.

- Chat via CLI, Python API, or HTTP
- One continuous agent-level message stream
- Speaker-aware messages via `user_id`
- Built-in Web UI and streaming responses
- Tool calling, MCP integration, and image input
- Long-term memory enabled by default with local or cloud storage

## Quick Start

### Install

```bash
pip install myxagent
```

### Set environment variables

```bash
export OPENAI_API_KEY=your_openai_api_key
```

### Start the CLI

```bash
xagent-cli
```

Single-shot mode:

```bash
xagent-cli --ask "Hello"
```

## Message Model

Every chat appends to the agent's continuous message stream.

- `user_id` identifies the current speaker
- recent context is pulled from the agent's global recent-message window
- single-user and multi-user interactions use the same runtime model

## HTTP API

Start the server:

```bash
xagent-server
```

Open the Web UI automatically:

```bash
xagent-server --open
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
    agent = Agent(model="gpt-5-mini")

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

## Configure with `agent.yaml`

Generate a starter config:

```bash
xagent-cli --init
```

Example:

```yaml
agent:
  name: "Assistant"
  system_prompt: |
    You are a helpful AI assistant.
    Answer clearly and accurately.
  model: "gpt-5-mini"

  capabilities:
    tools:
      - "web_search"

  storage_mode: "local"

server:
  host: "0.0.0.0"
  port: 8010
```

Run with config:

```bash
xagent-cli --config config/agent.yaml --toolkit_path my_toolkit
xagent-server --config config/agent.yaml --toolkit_path my_toolkit --open
```

## Cloud Mode

Use `storage_mode: "cloud"` when you want Redis-backed message storage and Upstash-backed memory.

```yaml
agent:
  storage_mode: "cloud"
```

Required environment variables:

```bash
export REDIS_URL=redis://localhost:6379/0
export UPSTASH_VECTOR_REST_URL=https://your-database.upstash.io
export UPSTASH_VECTOR_REST_TOKEN=your_token_here
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
