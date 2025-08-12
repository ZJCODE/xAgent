# xAgent - Multi-Modal AI Agent System

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)](https://fastapi.tiangolo.com/)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.28+-red.svg)](https://streamlit.io/)
[![Redis](https://img.shields.io/badge/Redis-7.0+-red.svg)](https://redis.io/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **🚀 A powerful multi-modal AI Agent system with modern architecture**

xAgent provides a complete AI assistant experience with text and image processing capabilities, intelligent vocabulary management, and high-performance concurrent tool execution. Built on FastAPI, Streamlit, and Redis for production-ready scalability.

## 📋 Table of Contents

- [🚀 Installation & Setup](#-installation--setup)
- [🌐 Quick Start: HTTP Agent Server](#-quick-start-http-agent-server)
- [🤖 Advanced Usage: Agent Class](#-advanced-usage-agent-class)
- [🎮 Full Project Experience](#-full-project-experience)
- [🏗️ Architecture](#%EF%B8%8F-architecture)
- [🔧 Development Guide](#-development-guide)
- [🤖 API Reference](#-api-reference)
- [📊 Monitoring & Observability](#-monitoring--observability)
- [🤝 Contributing](#-contributing)
- [📄 License](#-license)

## 🚀 Installation & Setup

### Prerequisites

| Requirement | Version | Purpose |
|-------------|---------|---------|
| **Python** | 3.12+ | Core runtime |
| **Redis** | 7.0+ | Message persistence |
| **OpenAI API Key** | - | AI model access |

### Install via pip

```bash
pip install myxagent
```

### Environment Configuration

Create a `.env` file in your project directory:

```bash
# Required
OPENAI_API_KEY=your_openai_api_key

# Optional - Redis persistence
REDIS_URL=your_redis_url_with_password

# Optional - Observability
LANGFUSE_SECRET_KEY=your_langfuse_key
LANGFUSE_PUBLIC_KEY=your_langfuse_public_key
LANGFUSE_HOST=https://cloud.langfuse.com

# Optional - Image upload to S3
AWS_ACCESS_KEY_ID=your_aws_access_key_id
AWS_SECRET_ACCESS_KEY=your_aws_secret_access_key
AWS_REGION=us-east-1
BUCKET_NAME=your_bucket_name
```

you can manually load the `.env` file into your shell:

```bash
export $(cat .env | grep -v '^#' | xargs)
```

## 🌐 Quick Start: HTTP Agent Server

The simplest way to use xAgent is through the HTTP server. Just create a config file and start serving!

### 1. Create Agent Configuration

Create `agent_config.yaml`:

```yaml
agent:
  name: "MyAgent"
  system_prompt: |
    You are a helpful assistant. Your task is to assist users with their queries and tasks.
  model: "gpt-4.1-mini"
  tools:
    - "web_search"  # Built-in web search
    - "draw_image"  # Built-in image generation (need set AWS credentials in .env)
    - "calculate_square"  # Custom tool from my_toolkit

server:
  host: "0.0.0.0"
  port: 8010
```

you can also add `mcp_servers` if you want to use MCP (Model Context Protocol) for dynamic tool loading:

```yaml
agent:
  ...
  mcp_servers:
    - "http://localhost:8001/mcp/"
  ...
```

you can also set `use_local_session` to `false` if you want to use Redis for session persistence(need to set `REDIS_URL` in `.env`):

```yaml
agent:
  ...
  use_local_session: false
  ...
```

### 2. Create Custom Tools (Optional)

Create `my_toolkit/` directory with `__init__.py` and your tool functions in script like  `your_tools.py`:

```python
# my_toolkit/__init__.py
from .your_tools import calculate_square, greet_user

# Agent will automatically discover these tools
TOOLKIT_REGISTRY = {
    "calculate_square": calculate_square,
    "greet_user": greet_user
}

```

implement your tools in `your_tools.py`:

```python
# my_toolkit/your_tools.py
from xagent.utils.tool_decorator import function_tool

@function_tool()
def calculate_square(n: int) -> int:
    """Calculate the square of a number."""
    return n * n

@function_tool()
def greet_user(name: str) -> str:
    """Greet a user by name."""
    return f"Hello, {name}! Nice to meet you."

```

### 3. Start the Server

```bash
# Start the HTTP Agent Server
xagent-server --config agent_config.yaml

# With custom toolkit (optional)
xagent-server --config agent_config.yaml --toolkit my_toolkit

# Server will be available at http://localhost:8010
```

### 4. Use the API

```bash
# Simple chat request
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user123",
    "session_id": "session456",
    "user_message": "Calculate the square of 15 and greet me as Alice"
  }'

# Streaming response
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user123",
    "session_id": "session456",
    "user_message": "Hello, how are you?",
    "stream": true
  }'
```

### 5. API Documentation

Visit `http://localhost:8010/docs` for interactive API documentation.

## 🤖 Advanced Usage: Agent Class

For more control and customization, use the Agent class directly in your Python code.

### Basic Agent Usage

```python
import asyncio
from xagent.core import Agent, Session

async def main():
    # Create agent
    agent = Agent(
        name="my_assistant",
        system_prompt="You are a helpful AI assistant.",
        model="gpt-4.1-mini"
    )

    # Create session for conversation management
    session = Session(session_id="session456")

    # Chat interaction
    response = await agent.chat("Hello, how are you?", session)
    print(response)

    # Streaming response example
    response = await agent.chat("Tell me a story", session, stream=True)
    async for event in response:
        print(event, end="")

asyncio.run(main())
```

### Adding Custom Tools

```python
import asyncio
import time
import httpx
from xagent.utils.tool_decorator import function_tool
from xagent.core import Agent, Session

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
    
    session = Session(user_id="user123")
    
    # Agent handles all tools automatically
    response = await agent.chat(
        "Calculate the square of 15 and get weather for Tokyo",
        session
    )
    print(response)

asyncio.run(main())
```

### Structured Outputs with Pydantic

```python
import asyncio
from pydantic import BaseModel
from xagent.core import Agent, Session
from xagent.tools import web_search

class WeatherReport(BaseModel):
    location: str
    temperature: int
    condition: str
    humidity: int

async def get_structured_response():
    agent = Agent(model="gpt-4.1-mini", tools=[web_search])
    session = Session(user_id="user123")
    
    # Request structured output
    weather_data = await agent.chat(
        "what's the weather like in Hangzhou?",
        session,
        output_type=WeatherReport
    )
    
    print(f"Location: {weather_data.location}")
    print(f"Temperature: {weather_data.temperature}°F")
    print(f"Condition: {weather_data.condition}")
    print(f"Humidity: {weather_data.humidity}%")

asyncio.run(get_structured_response())
```

### Agent as Tool Pattern

```python
import asyncio
from xagent.core import Agent, Session
from xagent.db import MessageDB
from xagent.tools import web_search

async def agent_as_tool_example():
    # Create specialized agents
    researcher_agent = Agent(
        name="research_specialist",
        system_prompt="Research expert. Gather information and provide insights.",
        model="gpt-4.1-mini",
        tools=[web_search]
    )
    
    # Convert agent to tool
    message_db = MessageDB()
    research_tool = researcher_agent.as_tool(
        name="researcher",
        description="Research topics and provide detailed analysis",
        message_db=message_db
    )
    
    # Main coordinator agent with specialist tools
    coordinator = Agent(
        name="coordinator",
        tools=[research_tool],
        system_prompt="Coordination agent that delegates to specialists.",
        model="gpt-4.1"
    )
    
    session = Session(user_id="user123")
    
    # Complex multi-step task
    response = await coordinator.chat(
        "Research renewable energy benefits and write a brief summary",
        session
    )
    print(response)

asyncio.run(agent_as_tool_example())
```

### Persistent Sessions with Redis

```python
import asyncio
from xagent.core import Agent, Session
from xagent.db import MessageDB

async def chat_with_persistence():
    # Initialize Redis-backed message storage
    message_db = MessageDB()
    
    # Create agent
    agent = Agent(
        name="persistent_agent",
        model="gpt-4.1-mini"
    )

    # Create session with Redis persistence
    session = Session(
        user_id="user123", 
        session_id="persistent_session",
        message_db=message_db
    )

    # Chat with automatic message persistence
    response = await agent.chat("Remember this: my favorite color is blue", session)
    print(response)
    
    # Later conversation - context is preserved in Redis
    response = await agent.chat("What's my favorite color?", session)
    print(response)

asyncio.run(chat_with_persistence())
```

## 🎮 Full Project Experience

If you want to experience the complete xAgent ecosystem with all features, clone the repository and use the provided scripts.

### Clone the Repository

```bash
git clone https://github.com/ZJCODE/xAgent.git
cd xAgent
pip install -r requirements.txt
```

### Environment Setup

```bash
# Copy and edit environment file
cp .env.example .env
# Edit .env with your API keys
```

### Quick Start (All Services)

```bash
chmod +x run.sh
./run.sh
```

This will start:
- **HTTP Agent Server** (http://localhost:8010) - Standalone agent API
- **MCP Server** (http://localhost:8001) - Model Context Protocol server
- **Chat Interface** (http://localhost:8501) - Streamlit web interface

### Manual Start (Individual Services)

```bash
# Terminal 1: Standalone HTTP Agent Server
python xagent/core/server.py --config config/agent.yaml --toolkit toolkit

# Terminal 2: MCP Server
python toolkit/mcp_server.py

# Terminal 3: Frontend
streamlit run frontend/chat_app.py --server.port 8501
```

### Access Points

| Service | URL | Description |
|---------|-----|-------------|
| **Chat Interface** | http://localhost:8501 | Main user interface |
| **API Docs** | http://localhost:8000/docs | Interactive API documentation |
| **HTTP Agent Server** | http://localhost:8010/chat | Standalone agent HTTP API |
| **Health Check** | http://localhost:8000/health | Service status monitoring |

## 🏗️ Architecture

**Modern Design for High Performance**

```
xAgent/
├── 🤖 xagent/                # Core async agent framework
│   ├── core/                 # Agent and session management
│   │   ├── agent.py          # Main Agent class with chat
│   │   ├── session.py        # Session management with operations
│   │   └── server.py         # Standalone HTTP Agent Server
│   ├── db/                   # Database layer (Redis)
│   │   └── message.py        # Message persistence
│   ├── schemas/              # Data models and types (Pydantic)
│   │   └── message.py        # Message and ToolCall models
│   ├── tools/                # Tool ecosystem
│   │   ├── __init__.py       # Tool registry (web_search, draw_image)
│   │   ├── openai_tool.py    # OpenAI tool integrations
│   │   └── mcp_demo/         # MCP demo server and client
│   └── utils/                # Utility functions
│       ├── tool_decorator.py # Tool decorators
│       ├── mcp_convertor.py  # MCP client
│       └── image_upload.py   # AWS S3 image upload utility
├── 🛠️ toolkit/               # Custom tool ecosystem
│   ├── __init__.py           # Toolkit registry
│   ├── tools.py              # Custom tools (char_count)
│   ├── mcp_server.py         # Main MCP server
│   └── vocabulary/           # Vocabulary learning system
├── ⚙️ config/                # Configuration files
│   └── agent.yaml            # Agent server configuration
├── 🎨 frontend/              # Streamlit web interface  
│   └── chat_app.py           # Main chat application
├── 📝 examples/              # Usage examples and demos
└── 🧪 tests/                 # Comprehensive test suite
```

### 🔄 Core Components

| Component | Purpose | Technology |
|-----------|---------|------------|
| **Agent** | Core conversation handler | OpenAI API + AsyncIO |
| **Session** | Message history management | Redis + Operations |
| **MessageDB** | Scalable persistence layer | Redis with client |
| **Tools** | Extensible function ecosystem | Auto sync-to-async conversion |
| **MCP** | Dynamic tool loading protocol | HTTP client |


### 🛠️ Creating Tools

Both sync and async functions work seamlessly:

```python
from xagent.utils.tool_decorator import function_tool
import asyncio
import time

# ✅ Sync tool - perfect for CPU-bound operations
@function_tool()
def my_sync_tool(input_text: str) -> str:
    """Process text synchronously (runs in thread pool)."""
    time.sleep(0.1)  # Simulate CPU-intensive work
    return f"Sync processed: {input_text}"

# ✅ Async tool - ideal for I/O-bound operations  
@function_tool()
async def my_async_tool(input_text: str) -> str:
    """Process text asynchronously."""
    await asyncio.sleep(0.1)  # Simulate async I/O operation
    return f"Async processed: {input_text}"
```

### 📋 Tool Development Guidelines

| Use Case | Tool Type | Example |
|----------|-----------|---------|
| **CPU-bound** | Sync functions | Math calculations, data processing |
| **I/O-bound** | Async functions | API calls, database queries |
| **Simple operations** | Sync functions | String manipulation, file operations |
| **Network requests** | Async functions | HTTP requests, WebSocket connections |

> **⚠️ Note**: Recursive functions are not supported as tools due to potential stack overflow issues in async environments.

### 🔄 Automatic Conversion

xAgent's `@function_tool()` decorator automatically handles sync-to-async conversion:

- **Sync functions** → Run in thread pool (non-blocking)
- **Async functions** → Run directly on event loop
- **Concurrent execution** → All tools execute in parallel when called

### 📝 Override Defaults

You can override the default tool name and description using the `function_tool` decorator:

```python
@function_tool(name="custom_square", description="Calculate the square of a number")
def calculate_square(n: int) -> int:
    return n * n
```

## 🤖 API Reference

### Core Classes

#### 🤖 Agent

Main AI agent class for handling conversations and tool execution.

```python
Agent(
    name: Optional[str] = None,
    system_prompt: Optional[str] = None, 
    model: Optional[str] = None,
    client: Optional[AsyncOpenAI] = None,
    tools: Optional[list] = None,
    mcp_servers: Optional[str | list] = None
)
```

**Key Methods:**
- `async chat(user_message, session, **kwargs) -> str | BaseModel`: Main chat interface
- `async __call__(user_message, session, **kwargs) -> str | BaseModel`: Shorthand for chat
- `as_tool(name, description, message_db) -> Callable`: Convert agent to tool

**Parameters:**
- `name`: Agent identifier (default: "default_agent")
- `system_prompt`: Instructions for the agent behavior
- `model`: OpenAI model to use (default: "gpt-4.1-mini")
- `client`: Custom AsyncOpenAI client instance
- `tools`: List of function tools
- `mcp_servers`: MCP server URLs for dynamic tool loading

#### 💬 Session

Manages conversation history and persistence with operations.

```python
Session(
    user_id: str,
    session_id: Optional[str] = None,
    message_db: Optional[MessageDB] = None
)
```

**Key Methods:**
- `async add_messages(messages: Message | List[Message]) -> None`: Store messages
- `async get_messages(count: int = 20) -> List[Message]`: Retrieve message history
- `async clear_session() -> None`: Clear conversation history
- `async pop_message() -> Optional[Message]`: Remove last non-tool message

#### 🗄️ MessageDB

Redis-backed message persistence layer.

```python
# Initialize with environment variables or defaults
message_db = MessageDB()

# Usage with session
session = Session(
    user_id="user123",
    message_db=message_db
)
```

### Important Considerations

| Aspect | Details |
|--------|---------|
| **Tool functions** | Can be sync or async (automatic conversion) |
| **Agent interactions** | Always use `await` |
| **Context** | Run in context with `asyncio.run()` |
| **Concurrency** | All tools execute in parallel automatically |

## 📊 Monitoring & Observability

xAgent includes comprehensive observability features:

- **🔍 Langfuse Integration** - Track AI interactions and performance
- **📝 Structured Logging** - Throughout the entire system
- **❤️ Health Checks** - API monitoring endpoints
- **⚡ Performance Metrics** - Tool execution time and success rates

## 🤝 Contributing

We welcome contributions! Here's how to get started:

### Development Workflow

1. **Fork** the repository
2. **Create** a feature branch: `git checkout -b feature/amazing-feature`
3. **Commit** your changes: `git commit -m 'Add amazing feature'`
4. **Push** to the branch: `git push origin feature/amazing-feature`
5. **Open** a Pull Request

### Development Guidelines

| Area | Requirements |
|------|-------------|
| **Code Style** | Follow PEP 8 standards |
| **Testing** | Add tests for new features |
| **Documentation** | Update docs as needed |
| **Type Safety** | Use type hints throughout |
| **Commits** | Follow conventional commit messages |

## Package Upload

First time upload

```bash
pip install build twine
python -m build
twine upload dist/*
```

Subsequent uploads

```bash
rm -rf dist/ build/ *.egg-info/
python -m build
twine upload dist/*
```

## 📄 License

This project is licensed under the **MIT License** - see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

Special thanks to the amazing open source projects that make xAgent possible:

- **[OpenAI](https://openai.com/)** - GPT models powering our AI
- **[FastAPI](https://fastapi.tiangolo.com/)** - Robust async API framework
- **[Streamlit](https://streamlit.io/)** - Intuitive web interface
- **[Redis](https://redis.io/)** - High-performance data storage
- **[Langfuse](https://langfuse.com/)** - Observability and monitoring

## 📞 Support & Community

| Resource | Link | Purpose |
|----------|------|---------|
| **🐛 Issues** | [GitHub Issues](https://github.com/ZJCODE/xAgent/issues) | Bug reports & feature requests |
| **💬 Discussions** | [GitHub Discussions](https://github.com/ZJCODE/xAgent/discussions) | Community chat & Q&A |
| **📧 Email** | zhangjun310@live.com | Direct support |

---

<div align="center">

**xAgent** - Empowering conversations with AI 🚀

[![GitHub stars](https://img.shields.io/github/stars/ZJCODE/xAgent?style=social)](https://github.com/ZJCODE/xAgent)
[![GitHub forks](https://img.shields.io/github/forks/ZJCODE/xAgent?style=social)](https://github.com/ZJCODE/xAgent)

*Built with ❤️ for the AI community*

</div>
