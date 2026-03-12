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
