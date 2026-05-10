# Getting Started

This guide gets xAgent running with the continuous agent message stream in a few minutes.

## Prerequisites

- Python 3.12+
- API key for an OpenAI-compatible provider

## Install

```bash
pip install myxagent
```

## Create a Config

```bash
xagent init
```

The init wizard asks for:

- provider: OpenAI, DeepSeek, Qwen, or custom
- model, with a "decide later" placeholder option
- API key, which can also be filled in later
- identity text for `identity.md`, or an empty placeholder to edit later

OpenAI config example:

```yaml
provider:
  base_url: "https://api.openai.com/v1"
  api_key: "your_api_key_here"
  model: "gpt-5.4-mini"
```

Edit `~/.xagent/identity.md` later if you skipped identity setup during init.

## Core Concepts

Each chat request includes:

- `user_id`: the current speaker

Single-user and multi-user interactions use the same runtime model. The only difference is how many distinct speakers appear in the agent's message stream.

## Start with the CLI

```bash
xagent chat
```

Single-shot mode:

```bash
xagent chat "Who are you?"
```

Inside the CLI:

- `clear`

## Start the HTTP Server

```bash
xagent server
```

- API base: `http://localhost:8010`
- Web UI: `http://localhost:8010`

Set host and port at startup:

```bash
xagent server --host 127.0.0.1 --port 8010
```

Example request:

```bash
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "alice",
    "user_message": "Hello"
  }'
```

Add another speaker to the same message stream:

```bash
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "bob",
    "user_message": "Summarize what Alice said."
  }'
```

## Use the Python API

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
        user_message="Summarize what the conversation has decided.",
        user_id="bob",
    )
    print(follow_up)


asyncio.run(main())
```

## Use Another xAgent Directory

```bash
xagent init --dir ./my-agent
xagent chat --dir ./my-agent "Hello"
xagent server --dir ./my-agent --host 0.0.0.0 --port 8010
```

The selected directory contains `config.yaml`, `identity.md`, and local xAgent runtime data.

## Next Reading

- [Configuration Reference](configuration_reference.md)
- [API Reference](api_reference.md)
- [Memory System](memory.md)
- [Best Practices](best_practices.md)
