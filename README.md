# xAgent - Multi-Modal AI Agent System

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)](https://fastapi.tiangolo.com/)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.28+-red.svg)](https://streamlit.io/)
[![Redis](https://img.shields.io/badge/Redis-7.0+-red.svg)](https://redis.io/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **🚀 A powerful and easy-to-use AI Agent system with real-time streaming responses**

xAgent provides a complete AI assistant experience with text and image processing, tool execution, HTTP server, web interface, and streaming CLI. Perfect for both quick prototypes and production deployments.

## 📋 Table of Contents

- [🚀 Quick Start](#-quick-start)
- [🔧 Installation](#-installation)
- [🌐 HTTP Server](#-http-server)
- [🌐 Web Interface](#-web-interface)
- [💻 Command Line Interface](#-command-line-interface)
- [🤖 Python API](#-python-api)
- [🔄 Multi-Agent Workflows](#-multi-agent-workflows)
- [📚 Documentation](#-documentation)
- [🤝 Contributing](#-contributing)
- [📄 License](#-license)


## 🚀 Quick Start

Get up and running with xAgent in just a few commands:

```bash
# Install xAgent
pip install myxagent

# Set your OpenAI API key
export OPENAI_API_KEY=your_openai_api_key

# Start interactive CLI chat
xagent-cli

# Or quickly ask a question
xagent-cli --ask "What's the weather like today?"

```

That's it!

### HTTP Server

The easiest way to deploy xAgent in production is through the HTTP server. Once you start the server, you can interact with your AI agent via HTTP API.

```bash
# Start the HTTP server
xagent-server

# Server runs at http://localhost:8010
```

Chat via HTTP API

```bash
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user123",
    "session_id": "session456", 
    "user_message": "Hello, how are you?"
  }'
```

Or launch web interface

```bash
xagent-web
```


## 🔧 Installation

### Prerequisites

- **Python 3.12+**
- **OpenAI API Key**

### Install from PyPI

```bash
# Latest version
pip install myxagent

# Upgrade existing installation
pip install --upgrade myxagent

# Using different mirrors
pip install myxagent -i https://pypi.org/simple
pip install myxagent -i https://mirrors.aliyun.com/pypi/simple  # China users
```

### Environment Setup

Create a `.env` file in your project directory:

```bash
# Required
OPENAI_API_KEY=your_openai_api_key

# Redis persistence
REDIS_URL=your_redis_url_with_password

# Observability
LANGFUSE_SECRET_KEY=your_langfuse_key
LANGFUSE_PUBLIC_KEY=your_langfuse_public_key
LANGFUSE_HOST=https://cloud.langfuse.com

# Image upload to S3
AWS_ACCESS_KEY_ID=your_aws_access_key_id
AWS_SECRET_ACCESS_KEY=your_aws_secret_access_key
AWS_REGION=us-east-1
BUCKET_NAME=your_bucket_name
```


## 🌐 HTTP Server

Here's how to run the xAgent HTTP server: 


### Quick Start

```bash
# Start with default settings
xagent-server

# Server runs at http://localhost:8010
```

### Basic Configuration

Create `agent_config.yaml`:

```yaml
agent:
  name: "MyAgent"
  system_prompt: "You are a helpful assistant"
  model: "gpt-4.1-mini"
  capabilities:
    tools:
      - "web_search"      # Built-in web search
      - "draw_image"      # Built-in image generation (need set aws credentials for image upload)
      - "custom_tool"     # Your custom tools

server:
  host: "0.0.0.0"
  port: 8010
```

more advanced configurations can be found in [Configuration Reference](docs/configuration_reference.md).

### Custom Tools

Create custom tools in `my_toolkit/`:

```python
# my_toolkit/__init__.py
from .tools import *

TOOLKIT_REGISTRY = {
    "calculate_square": calculate_square,
    "fetch_weather": fetch_weather
}

# my_toolkit/tools.py
from xagent.utils.tool_decorator import function_tool

@function_tool()
def calculate_square(n: int) -> int:
    """Calculate the square of a number."""
    return n * n

@function_tool()
async def fetch_weather(city: str) -> str:
    """Fetch weather data from an API."""
    # Simulate API call
    await asyncio.sleep(0.5)
    return f"Weather in {city}: 22°C, Sunny"
```

### Start with Custom Config

```bash
xagent-server --config agent_config.yaml --toolkit_path my_toolkit
```

### API Usage

```bash
# Simple chat
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user123",
    "session_id": "session456", 
    "user_message": "Calculate the square of 15"
  }'

# Streaming response
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user123",
    "session_id": "session456",
    "user_message": "Tell me a story",
    "stream": true
  }'
```

For advanced configurations including multi-agent systems and structured outputs, see [Configuration Reference](docs/configuration_reference.md).

## 🌐 Web Interface

User-friendly Streamlit chat interface for interactive conversations with your AI agent.

```bash
# Start the chat interface with default settings
xagent-web

# With custom agent server URL
xagent-web --agent-server http://localhost:8010

# With custom host and port
xagent-web --host 0.0.0.0 --port 8501 --agent-server http://localhost:8010
```

## 💻 Command Line Interface

Interactive CLI with real-time streaming for quick testing and development.

### Basic Usage

```bash
# Interactive chat mode (default with streaming)
xagent-cli

# Ask a single question
xagent-cli --ask "What is the capital of France?"

# Use custom configuration
xagent-cli --config my_config.yaml --toolkit_path my_toolkit
```

### Interactive Chat Session

```bash
$ xagent-cli
🤖 Welcome to xAgent CLI!
Agent: Agent | Model: gpt-4.1-mini | Tools: 3 loaded
Session: cli_session_abc123 | Streaming: Enabled
--------------------------------------------------

👤 You: Hello, how are you?
🤖 Agent: Hello! I'm doing well, thank you for asking...

👤 You: help
📋 Available commands:
  exit, quit, bye  - Exit the chat session
  clear           - Clear session history  
  stream on/off   - Toggle streaming mode
  help            - Show this help message

👤 You: exit
👋 Goodbye!
```

### CLI Options

| Option | Description | Example |
|--------|-------------|---------|
| `--config` | Configuration file path | `--config my_config.yaml` |
| `--toolkit_path` | Custom toolkit directory | `--toolkit_path my_toolkit` |
| `--ask` | Ask single question and exit | `--ask "Hello world"` |
| `--verbose` | Enable verbose logging | `--verbose` |
| `--no-stream` | Disable streaming | `--no-stream` |

## 🤖 Python API

Use xAgent directly in your Python applications with full control and customization.

### Basic Usage

```python
import asyncio
from xagent.core import Agent

async def main():
    # Create agent
    agent = Agent(
        name="my_assistant",
        system_prompt="You are a helpful AI assistant.",
        model="gpt-4.1-mini"
    )

    # Chat interaction
    response = await agent.chat(
        user_message="Hello, how are you?",
        user_id="user123", 
        session_id="session456"
    )
    print(response)

asyncio.run(main())
```

### Streaming Responses

```python
async def streaming_example():
    agent = Agent()
    
    response = await agent.chat(
        user_message="Tell me a story",
        user_id="user123",
        session_id="session456",
        stream=True
    )
    
    async for chunk in response:
        print(chunk, end="")

asyncio.run(streaming_example())
```

For more advanced examples including multi-agent systems and workflow orchestration, see [Configuration Reference](docs/configuration_reference.md).

### Adding Custom Tools

```python
import asyncio
import time
import httpx
from xagent.utils.tool_decorator import function_tool
from xagent.core import Agent

# Sync tools - automatically converted to async
@function_tool()
def calculate_square(n: int) -> int:
    """Calculate square of a number."""
    time.sleep(0.1)  # Simulate CPU work
    return n * n

# Async tools - used directly for I/O operations
@function_tool()
async def fetch_weather(city: str) -> str:
    """Fetch weather data from API."""
    async with httpx.AsyncClient() as client:
        await asyncio.sleep(0.5)  # Simulate API call
        return f"Weather in {city}: 22°C, Sunny"

async def main():
    # Create agent with custom tools
    agent = Agent(
        tools=[calculate_square, fetch_weather],
        model="gpt-4.1-mini"
    )
    
    # Agent handles all tools automatically
    response = await agent.chat(
        user_message="Calculate the square of 15 and get weather for Tokyo",
        user_id="user123",
        session_id="session456"
    )
    print(response)

asyncio.run(main())
```

### Structured Outputs

With Pydantic structured outputs, you can:
- Parse and validate an agent’s response into typed data
- Easily extract specific fields
- Ensure the response matches the expected format
- Guarantee type safety in your application
- Reliably chain multi-step tasks using structured data

```python
import asyncio
from pydantic import BaseModel
from xagent.core import Agent
from xagent.tools import web_search

class WeatherReport(BaseModel):
    location: str
    temperature: int
    condition: str
    humidity: int

class Step(BaseModel):
    explanation: str
    output: str

class MathReasoning(BaseModel):
    steps: list[Step]
    final_answer: str


async def get_structured_response():
    
    agent = Agent(model="gpt-4.1-mini", 
                  tools=[web_search], 
                  output_type=WeatherReport) # You can set a default output type here or leave it None
    
    # Request structured output for weather
    weather_data = await agent.chat(
        user_message="what's the weather like in Hangzhou?",
        user_id="user123",
        session_id="session456"
    )
    
    print(f"Location: {weather_data.location}")
    print(f"Temperature: {weather_data.temperature}°F")
    print(f"Condition: {weather_data.condition}")
    print(f"Humidity: {weather_data.humidity}%")


    # Request structured output for mathematical reasoning (overrides output_type)
    reply = await agent.chat(
        user_message="how can I solve 8x + 7 = -23",
        user_id="user123",
        session_id="session456",
        output_type=MathReasoning
    ) # Override output_type for this call
    for index, step in enumerate(reply.steps):
        print(f"Step {index + 1}: {step.explanation} => Output: {step.output}")
    print("Final Answer:", reply.final_answer)

if __name__ == "__main__":
    asyncio.run(get_structured_response())
```

### Agent as Tool Pattern

```python
import asyncio
from xagent.core import Agent
from xagent.components import MessageStorageLocal
from xagent.tools import web_search

async def agent_as_tool_example():
    # Create specialized agents with message storage
    message_storage = MessageStorageLocal()
    
    researcher_agent = Agent(
        name="research_specialist",
        system_prompt="Research expert. Gather information and provide insights.",
        model="gpt-4.1-mini",
        tools=[web_search],
        message_storage=message_storage
    )
    
    # Convert agent to tool
    research_tool = researcher_agent.as_tool(
        name="researcher",
        description="Research topics and provide detailed analysis"
    )
    
    # Main coordinator agent with specialist tools
    coordinator = Agent(
        name="coordinator",
        tools=[research_tool],
        system_prompt="Coordination agent that delegates to specialists.",
        model="gpt-4.1",
        message_storage=message_storage
    )
    
    # Complex multi-step task
    response = await coordinator.chat(
        user_message="Research renewable energy benefits and write a brief summary",
        user_id="user123",
        session_id="session456"
    )
    print(response)

asyncio.run(agent_as_tool_example())
```

### Persistent Sessions with Redis

```python
import asyncio
from xagent.core import Agent
from xagent.components import MessageStorageRedis

async def chat_with_persistence():
    # Initialize Redis-backed message storage
    message_storage = MessageStorageRedis()
    
    # Create agent with Redis persistence
    agent = Agent(
        name="persistent_agent",
        model="gpt-4.1-mini",
        message_storage=message_storage
    )

    # Chat with automatic message persistence
    response = await agent.chat(
        user_message="Remember this: my favorite color is blue",
        user_id="user123",
        session_id="persistent_session"
    )
    print(response)
    
    # Later conversation - context is preserved in Redis
    response = await agent.chat(
        user_message="What's my favorite color?",
        user_id="user123",
        session_id="persistent_session"
    )
    print(response)

asyncio.run(chat_with_persistence())
```

you can implement your own message storage by inheriting from `MessageStorageBase` and implementing the required methods like `add_messages`, `get_messages`, etc.

## 🔄 Multi-Agent Workflows

xAgent enables powerful multi-agent coordination patterns for complex tasks.

### Workflow Patterns

| Pattern | Use Case | Example |
|---------|----------|---------|
| **Sequential** | Pipeline processing | Research -> Analysis -> Summary |
| **Parallel** | Multiple perspectives | Expert panel reviews |
| **Graph** | Complex dependencies | A->B, A->C, B&C->D |
| **Hybrid** | Multi-stage workflows | Sequential + Parallel stages |

### Quick Example

```python
import asyncio
from xagent import Agent
from xagent.multi.workflow import Workflow

async def workflow_example():
    # Create specialized agents
    researcher = Agent(name="Researcher", system_prompt="Research specialist")
    analyst = Agent(name="Analyst", system_prompt="Data analysis expert")
    writer = Agent(name="Writer", system_prompt="Content writing specialist")
    
    workflow = Workflow()
    
    # Sequential workflow
    result = await workflow.run_sequential(
        agents=[researcher, analyst, writer],
        task="Research AI trends and write a summary report"
    )
    print("Result:", result.result)
    
    # Graph workflow with intuitive DSL
    dependencies = "Researcher->Analyst, Researcher&Analyst->Writer"
    result = await workflow.run_graph(
        agents=[researcher, analyst, writer],
        dependencies=dependencies,
        task="Create comprehensive market analysis"
    )
    print("Graph result:", result.result)

asyncio.run(workflow_example())
```

### DSL Syntax

Use intuitive arrow notation for complex dependencies:

```python
# Simple chain
"A->B->C"

# Parallel branches
"A->B, A->C"

# Fan-in pattern
"A->C, B->C"

# Complex dependencies
"A->B, A->C, B&C->D"
```

For detailed workflow patterns and examples, see [Multi-Agent Workflows](docs/workflows.md) and [Workflow DSL](docs/workflow_dsl.md).

## 📚 Documentation

### Core Documentation
- [Configuration Reference](docs/configuration_reference.md) - Complete YAML configuration guide and examples
- [API Reference](docs/api_reference.md) - Complete API documentation and parameter reference
- [Multi-Agent Workflows](docs/workflows.md) - Workflow patterns and orchestration examples
- [Workflow DSL](docs/workflow_dsl.md) - Domain-specific language for defining agent dependencies

### Examples
- [examples/](examples/) - Complete usage examples and demos
- [config/](config/) - Configuration file templates
- [toolkit/](toolkit/) - Custom tool development examples

### Architecture Overview

xAgent is built with a modular architecture:

- **Core (`xagent/core/`)** - Agent and session management
- **Interfaces (`xagent/interfaces/`)** - CLI, HTTP server, and web interface
- **Components (`xagent/components/`)** - Message storage and persistence
- **Tools (`xagent/tools/`)** - Built-in and custom tool ecosystem
- **Multi (`xagent/multi/`)** - Multi-agent coordination and workflows

### Key Features

- **� Easy to Use** - Simple API for quick prototyping
- **⚡ High Performance** - Async/await throughout, concurrent tool execution
- **🔧 Extensible** - Custom tools, MCP integration, plugin system
- **🌐 Multiple Interfaces** - CLI, HTTP API, web interface
- **💾 Persistent** - Redis-backed conversation storage
- **🤖 Multi-Agent** - Hierarchical agent systems and workflows
- **📊 Observable** - Built-in logging and monitoring
```

**Key Methods:**
- `async chat(user_message, user_id, session_id, **kwargs) -> str | BaseModel`: Main chat interface
- `async __call__(user_message, user_id, session_id, **kwargs) -> str | BaseModel`: Shorthand for chat
- `as_tool(name, description) -> Callable`: Convert agent to tool

**Chat Method Parameters:**
- `user_message`: The user's message (string or Message object)
- `user_id`: User identifier for message storage (default: "default_user")
- `session_id`: Session identifier for message storage (default: "default_session")
- `history_count`: Number of previous messages to include (default: 16)
- `max_iter`: Maximum model call attempts (default: 10)
- `image_source`: Optional image for analysis (URL, path, or base64)
- `output_type`: Pydantic model for structured output
- `stream`: Enable streaming response (default: False)

**Agent Parameters:**
- `name`: Agent identifier (default: "default_agent")
- `system_prompt`: Instructions for the agent behavior
- `model`: OpenAI model to use (default: "gpt-4.1-mini")
- `client`: Custom AsyncOpenAI client instance
- `tools`: List of function tools
- `mcp_servers`: MCP server URLs for dynamic tool loading
- `sub_agents`: List of sub-agent configurations (name, description, server URL)
## 🤝 Contributing

We welcome contributions! Here's how to get started:

### Development Workflow

1. **Fork** the repository
2. **Create** a feature branch: `git checkout -b feature/amazing-feature`  
3. **Commit** your changes: `git commit -m 'Add amazing feature'`
4. **Push** to the branch: `git push origin feature/amazing-feature`
5. **Open** a Pull Request

### Development Guidelines

- Follow PEP 8 standards for code style
- Add tests for new features
- Update documentation as needed
- Use type hints throughout
- Follow conventional commit messages

## 📄 License

This project is licensed under the **MIT License** - see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

Special thanks to these amazing open source projects:

- **[OpenAI](https://openai.com/)** - GPT models powering our AI
- **[FastAPI](https://fastapi.tiangolo.com/)** - Robust async API framework  
- **[Streamlit](https://streamlit.io/)** - Intuitive web interface
- **[Redis](https://redis.io/)** - High-performance data storage

## 📞 Support & Community

| Resource | Purpose |
|----------|---------|
| **[GitHub Issues](https://github.com/ZJCODE/xAgent/issues)** | Bug reports & feature requests |
| **[GitHub Discussions](https://github.com/ZJCODE/xAgent/discussions)** | Community chat & Q&A |
| **Email: zhangjun310@live.com** | Direct support |

---

<div align="center">

**xAgent** - Empowering conversations with AI 🚀

[![GitHub stars](https://img.shields.io/github/stars/ZJCODE/xAgent?style=social)](https://github.com/ZJCODE/xAgent)
[![GitHub forks](https://img.shields.io/github/forks/ZJCODE/xAgent?style=social)](https://github.com/ZJCODE/xAgent)

*Built with ❤️ for the AI community*

</div>
