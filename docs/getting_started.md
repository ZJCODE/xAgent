# Getting Started

This guide gets xAgent running with the continuous agent message stream in a few minutes.

## Prerequisites

- Python 3.12+
- OpenAI API key

## Install

```bash
pip install myxagent
```

## Set Environment Variables

```bash
export OPENAI_API_KEY=your_openai_api_key
```

If you plan to use cloud storage, also see [README Cloud Mode](../README.md#cloud-mode).

## Core Concepts

Each chat request includes:

- `user_id`: the current speaker

Single-user and multi-user interactions use the same runtime model. The only difference is how many distinct speakers appear in the agent's message stream.

## Start with the CLI

```bash
xagent-cli
```

Single-shot mode:

```bash
xagent-cli --ask "Who are you?"
```

Inside the CLI:

- `clear`

## Start the HTTP Server

```bash
xagent-server
```

- API base: `http://localhost:8010`
- Web UI: `http://localhost:8010`

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
    agent = Agent(model="gpt-5-mini")

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

## Generate a Starter Config

```bash
xagent-cli --init
```

This creates:

- `config/agent.yaml`
- `my_toolkit/` with starter custom tools

Then run:

```bash
xagent-server --config config/agent.yaml --toolkit_path my_toolkit
```

## Next Reading

- [Configuration Reference](configuration_reference.md)
- [API Reference](api_reference.md)
- [Memory System](memory.md)
- [Best Practices](best_practices.md)
