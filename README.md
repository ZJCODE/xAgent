# xAgent

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)](https://fastapi.tiangolo.com/)
[![Redis](https://img.shields.io/badge/Redis-7.0+-red.svg)](https://redis.io/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A production-ready AI Agent framework focused on **easy start** and **scalable deployment**.

- ✅ Chat via CLI / Python API / HTTP Server
- ✅ Built-in Web UI and streaming responses
- ✅ Tool calling, MCP integration, image input
- ✅ Multi-user + multi-session support
- ✅ Memory and multi-agent workflows

## 3-Minute Quick Start

### 1) Install

```bash
pip install myxagent
```

### 2) Set environment variable

```bash
export OPENAI_API_KEY=your_openai_api_key
```

### 3) Start using xAgent

```bash
# Interactive CLI
xagent-cli

# Or ask one question
xagent-cli --ask "Hello"
```

## Configure with `agent.yaml`

If you want to customize the agent prompt, model, tools, or server port, create a YAML config file.

### 1) Generate a starter config

```bash
xagent-cli --init
```

This creates:

- `config/agent.yaml`
- `my_toolkit/` for custom tools

### 2) Edit `config/agent.yaml`

```yaml
agent:
    name: "Assistant"
    system_prompt: |
        You are a helpful AI assistant.
        Answer clearly and accurately.
    model: "gpt-4.1-mini"

    capabilities:
        tools:
            - "web_search"

    message_storage: "local"

server:
    host: "0.0.0.0"
    port: 8010
```

### 3) Run with your config

```bash
# CLI
xagent-cli --config config/agent.yaml --toolkit_path my_toolkit

# HTTP Server + Web UI
xagent-server --config config/agent.yaml --toolkit_path my_toolkit --open
```

If you do not use custom tools, you can omit `--toolkit_path`.

For more YAML options, see [docs/configuration_reference.md](docs/configuration_reference.md).

## Most Common Usage

### CLI

```bash
xagent-cli
xagent-cli --ask "What is the weather in Hangzhou?"
```

### HTTP Server (API + Web UI)

```bash
xagent-server
# http://localhost:8010
```

```bash
# Open Web UI automatically
xagent-server --open
```

```bash
# API call example
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user123",
    "session_id": "session456",
    "user_message": "Hello"
  }'
```

```bash
# Continuous conversation: keep same user_id + session_id
# Turn 1
curl -X POST "http://localhost:8010/chat" \
    -H "Content-Type: application/json" \
    -d '{
        "user_id": "alice",
        "session_id": "daily_chat",
        "user_message": "Remember that my favorite city is Hangzhou."
    }'

# Turn 2 (same session)
curl -X POST "http://localhost:8010/chat" \
    -H "Content-Type: application/json" \
    -d '{
        "user_id": "alice",
        "session_id": "daily_chat",
        "user_message": "What is my favorite city?"
    }'
```

```bash
# Image input via image_source (single image)
curl -X POST "http://localhost:8010/chat" \
    -H "Content-Type: application/json" \
    -d '{
        "user_id": "user123",
        "session_id": "image_session",
        "user_message": "Describe this image.",
        "image_source": "https://example.com/image.jpg"
    }'
```

```bash
# Image input via image_source (multiple images)
curl -X POST "http://localhost:8010/chat" \
    -H "Content-Type: application/json" \
    -d '{
        "user_id": "user123",
        "session_id": "image_session",
        "user_message": "Compare these images.",
        "image_source": [
            "https://example.com/image1.jpg",
            "https://example.com/image2.jpg"
        ]
    }'
```

```bash
# Image URL directly in message text (no image_source needed)
curl -X POST "http://localhost:8010/chat" \
    -H "Content-Type: application/json" \
    -d '{
        "user_id": "user123",
        "session_id": "image_in_message",
        "user_message": "What do you see in this image? https://example.com/cat.jpg"
    }'
```

### Python API

```python
import asyncio
from xagent.core import Agent

async def main():
    agent = Agent(model="gpt-4.1-mini")
    response = await agent.chat(
        user_message="Hello",
        user_id="user123",
        session_id="session456"
    )
    print(response)

asyncio.run(main())
```

### Continuous Conversation (same session)

Use the same `user_id` and `session_id` to keep context across turns:

```python
import asyncio
from xagent.core import Agent

async def main():
    agent = Agent(model="gpt-4.1-mini")

    user_id = "alice"
    session_id = "daily_chat"

    reply1 = await agent.chat(
        user_message="Remember that my favorite city is Hangzhou.",
        user_id=user_id,
        session_id=session_id,
    )
    print("Turn 1:", reply1)

    reply2 = await agent.chat(
        user_message="What is my favorite city?",
        user_id=user_id,
        session_id=session_id,
    )
    print("Turn 2:", reply2)

asyncio.run(main())
```

### Image Input Support

`image_source` supports a single value or list, and each item can be an image URL, local file path, or base64 data URI.

```python
import asyncio
from xagent.core import Agent

async def main():
    agent = Agent(model="gpt-4.1-mini")

    # Single image URL
    reply1 = await agent.chat(
        user_message="What do you see in this image?",
        user_id="user123",
        session_id="image_demo",
        image_source="https://example.com/image.jpg",
    )
    print("Single image:", reply1)

    # Multiple images (URL + local path)
    reply2 = await agent.chat(
        user_message="Compare these two images.",
        user_id="user123",
        session_id="image_demo",
        image_source=[
            "https://example.com/image1.jpg",
            "./local_image.png",
        ],
    )
    print("Multi-image:", reply2)

asyncio.run(main())
```

## Recommended Learning Path

1. **Quickly run it**: this README
2. **Project setup + config**: `xagent-cli --init`
3. **Pick your interface**: CLI / HTTP / Python API
4. **Then add advanced capabilities**: memory, workflows, custom tools

## Documentation Center

All technical details are now organized in `docs/`:

### Start Here
- [Documentation Index](docs/README.md)
- [Getting Started](docs/getting_started.md)
- [Best Practices](docs/best_practices.md)

### Core References
- [Configuration Reference](docs/configuration_reference.md)
- [API Reference](docs/api_reference.md)
- [Memory System](docs/memory.md)
- [Multi-Agent Workflows](docs/workflows.md)
- [Workflow DSL](docs/workflow_dsl.md)

### Advanced / Deployment
- [HTTP Server Agent Passing](docs/http_server_agent_passing.md)
- [Message Storage Inheritance](docs/message_storage_inheritance.md)
- [Redis Cluster Support](docs/redis_cluster_support.md)
- [Docker Deployment](deploy/docker/README.md)

### Examples
- [Demo Examples](examples/demo)
- [Config Examples](examples/config)
- [Toolkit Examples](examples/toolkit)

## Contributing

Contributions are welcome. Please open an issue or pull request on GitHub.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
