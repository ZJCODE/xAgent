# Getting Started

This guide helps you go from zero to a working xAgent setup in a few minutes.

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

If you plan to use cloud mode, follow the unified checklist in [README Cloud Mode](../README.md#cloud-mode).

## Choose One Start Mode

### Option A: CLI (fastest)

```bash
xagent-cli
```

Single-shot mode:

```bash
xagent-cli --ask "Who are you?"
```

### Option B: HTTP API + Web UI

```bash
xagent-server
```

- API base: `http://localhost:8010`
- Web UI: `http://localhost:8010` (or run `xagent-server --open`)

API request example:

```bash
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user123",
    "session_id": "session456",
    "user_message": "Hello"
  }'
```

### Option C: Python API

```python
import asyncio
from xagent.core import Agent

async def main():
    agent = Agent(model="gpt-5-mini")
    reply = await agent.chat(
        user_message="Hello",
        user_id="user123",
        session_id="session456",
    )
    print(reply)

asyncio.run(main())
```

## Initialize Project Structure (Recommended)

```bash
xagent-cli --init
```

This creates:

- `config/agent.yaml`
- `my_toolkit/` with starter custom tools

Then run with project config:

```bash
xagent-server --config config/agent.yaml --toolkit_path my_toolkit
```

## What to Read Next

- Need config details: [Configuration Reference](configuration_reference.md)
- Need Python method details: [API Reference](api_reference.md)
- Need custom memory: [Memory System](memory.md)
- Need multi-agent orchestration: [Multi-Agent Workflows](workflows.md)
- Need practical guidance: [Best Practices](best_practices.md)
