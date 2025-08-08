# xAgent - Multi-Modal Conversational AI System

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)](https://fastapi.tiangolo.com/)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.28+-red.svg)](https://streamlit.io/)
[![Redis](https://img.shields.io/badge/Redis-7.0+-red.svg)](https://redis.io/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **🚀 A powerful multi-modal conversational AI system with modern architecture**

xAgent provides a complete AI assistant experience with text and image processing capabilities, intelligent vocabulary management, and high-performance concurrent tool execution. Built on FastAPI, Streamlit, and Redis for production-ready scalability.

## 📋 Table of Contents

- [✨ Key Features](#-key-features)
- [🏗️ Architecture](#%EF%B8%8F-architecture)
- [🚀 Quick Start](#-quick-start)
- [💡 Usage Examples](#-usage-examples)
  - [📘 Basic Chat](#-basic-chat)
  - [🗄️ Advanced Chat with Redis Persistence](#%EF%B8%8F-advanced-chat-with-redis-persistence)
  - [🔧 Custom Tools (Sync and Async)](#-custom-tools-sync-and-async)
  - [🔧 MCP Protocol Integration](#-mcp-protocol-integration)
  - [📊 Structured Output with Pydantic](#-structured-output-with-pydantic)
  - [🤖 Agent as Tool Pattern](#-agent-as-tool-pattern)
- [🌐 HTTP Agent Server](#-http-agent-server)
- [🔧 Development Guide](#-development-guide)
  - [🛠️ Creating Tools](#%EF%B8%8F-creating-tools)
  - [📋 Tool Development Guidelines](#-tool-development-guidelines)
  - [🔄 Automatic Conversion](#-automatic-conversion)
- [🤖 API Reference](#-api-reference)
- [📊 Monitoring & Observability](#-monitoring--observability)
- [🤝 Contributing](#-contributing)
- [📄 License](#-license)

## ✨ Key Features

### 🤖 **Multi-Modal AI Chat**
- **Text Conversations** - OpenAI GPT models (GPT-4.1, GPT-4.1-mini, GPT-4o)
- **Image Processing** - Upload and analyze images with AI
- **Session Management** - Persistent conversation history with Redis
- **Tool Integration** - Extensible tool system with auto sync-to-async conversion
- **MCP Support** - Model Context Protocol for dynamic tool loading
- **Concurrent Execution** - Parallel tool execution for improved performance


### 🔧 **Developer Experience**
- **Modern Design** - High-performance concurrent operations
- **Modular Architecture** - Clean separation with pluggable components
- **Type Safety** - Full type hints with Pydantic models
- **Comprehensive Testing** - Full test coverage with pytest
- **Observability** - Built-in logging and monitoring with Langfuse
- **Flexible Tools** - Support both sync and async tool functions


## 🏗️ Architecture

**Modern Design for High Performance**

```
xAgent/
├── 🌐 api/                    # FastAPI backend services
│   ├── main.py               # API server entry point
│   ├── health.py             # Health check endpoints
│   └── schemas/              # API data models
├── 🎨 frontend/              # Streamlit web interface  
│   └── chat_app.py           # Main chat application
├── 🤖 xagent/                # Core async agent framework
│   ├── core/                 # Agent and session management
│   │   ├── agent.py          # Main Agent class with chat
│   │   ├── session.py        # Session management with operations
│   │   └── server.py         # Standalone HTTP Agent Server
│   ├── db/                   # Database layer (Redis)
│   │   └── message.py        # Message persistence
│   ├── schemas/              # Data models and types (Pydantic)
│   ├── tools/                # Tool ecosystem
│   │   ├── mcp_server.py     # MCP protocol server
│   │   ├── openai_tool.py    # OpenAI tool integrations
│   │   └── vocabulary/       # Vocabulary learning system
│   └── utils/                # Utility functions
│       ├── tool_decorator.py # Tool decorators
│       └── mcp_convertor.py  # MCP client
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

## 🚀 Quick Start

### Prerequisites

| Requirement | Version | Purpose |
|-------------|---------|---------|
| **Python** | 3.12+ | Core runtime |
| **Redis** | 7.0+ | Message persistence |
| **OpenAI API Key** | - | AI model access |

### Installation

Clone and Setup
```bash
git clone https://github.com/ZJCODE/xAgent.git
cd xAgent
pip install -r requirements.txt
```

Environment Configuration
```bash
# Copy and edit environment file
cp .env.example .env
```

Required variables
```env
OPENAI_API_KEY=your_openai_api_key
```

Optional variables
```env
REDIS_URL=your_redis_url_with_password
LANGFUSE_SECRET_KEY=your_langfuse_key
LANGFUSE_PUBLIC_KEY=your_langfuse_public_key
LANGFUSE_HOST=https://cloud.langfuse.com
AWS_ACCESS_KEY_ID=your_aws_access_key_id
AWS_SECRET_ACCESS_KEY=your_aws_secret_access_key
AWS_REGION=us-east-1
BUCKET_NAME=your_bucket_name
```

### Running the Application

#### 🚀 Quick Start (All Services)

```bash
chmod +x run.sh
./run.sh
```

#### ⚙️ Manual Start (Individual Services)

```bash
# Terminal 1: API Server
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000

# Terminal 2: MCP Server  
python xagent/tools/mcp_server.py

# Terminal 3: Frontend
streamlit run frontend/chat_app.py --server.port 8501

# Terminal 4: Standalone HTTP Agent Server (Optional)
python xagent/core/server.py --config config/agent.yaml
```


### 🌐 Access Points

| Service | URL | Description |
|---------|-----|-------------|
| **Chat Interface** | http://localhost:8501 | Main user interface |
| **API Docs** | http://localhost:8000/docs | Interactive API documentation |
| **Health Check** | http://localhost:8000/health | Service status monitoring |
| **HTTP Agent Server** | http://localhost:8010/chat | Standalone agent HTTP API |

## 💡 Usage Examples

### 📘 Basic Chat

```python
import asyncio
from xagent.core import Agent, Session
from xagent.tools import web_search

async def main():
    # Create agent with modern architecture
    agent = Agent(
        name="my_assistant",
        system_prompt="You are a helpful AI assistant.",
        model="gpt-4.1-mini",
        tools=[web_search]  # Add web search tool
    )

    # Create session for conversation management
    session = Session(session_id="session456")

    # Chat interaction
    response = await agent.chat("Hello, how are you?", session)
    print(response)

    # Continue conversation with context
    response = await agent.chat("What's the weather like in Hangzhou?", session)
    print(response)

asyncio.run(main())
```

### 🗄️ Advanced Chat with Redis Persistence

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
        model="gpt-4.1-mini",
        tools=[]
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

### 🔧 Custom Tools (Sync and Async)

```python
import asyncio
import time
import httpx
from xagent.utils.tool_decorator import function_tool
from xagent.core import Agent, Session

# Sync tools - automatically converted to async
@function_tool()
def calculate_square(n: int) -> int:
    """Calculate square of a number (CPU-intensive)."""
    time.sleep(0.1)  # Simulate CPU work
    return n * n

@function_tool()
def format_text(text: str, style: str) -> str:
    """Format text with various styles."""
    if style == "upper":
        return text.upper()
    elif style == "title":
        return text.title()
    return text

# Async tools - used directly for I/O operations
@function_tool()
async def fetch_weather(city: str) -> str:
    """Fetch weather data from API."""
    async with httpx.AsyncClient() as client:
        # Simulate weather API call
        await asyncio.sleep(0.5)
        return f"Weather in {city}: 22°C, Sunny"

async def main():
    # Mix of sync and async tools
    agent = Agent(
        tools=[calculate_square, format_text, fetch_weather],
        model="gpt-4.1-mini"
    )
    
    session = Session(user_id="user123")
    
    # Agent handles all tools automatically - sync tools run in thread pool
    response = await agent.chat(
        "Calculate the square of 15, format 'hello world' in title case, and get weather for Tokyo",
        session
    )
    print(response)

asyncio.run(main())
```

### 🔧 MCP Protocol Integration

```python
import asyncio
from xagent.core import Agent, Session

async def mcp_integration_example():
    # Create agent with MCP tools
    agent = Agent(
        tools=[],
        mcp_servers=["http://localhost:8001/mcp/"],  # Auto-refresh MCP tools
        model="gpt-4.1-mini"
    )
    
    session = Session(user_id="user123")
    
    # Use MCP tools automatically
    response = await agent.chat("Use the available MCP tools to help me", session)
    print(response)

asyncio.run(mcp_integration_example())
```

### 📊 Structured Output with Pydantic

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

class Step(BaseModel):
    explanation: str
    output: str

class MathReasoning(BaseModel):
    steps: list[Step]
    final_answer: str

async def get_structured_response():
    agent = Agent(model="gpt-4.1-mini", tools=[web_search])
    session = Session(user_id="user123")
    
    # Request structured output for weather
    weather_data = await agent.chat(
        "what's the weather like in Hangzhou?",
        session,
        output_type=WeatherReport
    )
    
    print(f"Location: {weather_data.location}")
    print(f"Temperature: {weather_data.temperature}°F")
    print(f"Condition: {weather_data.condition}")
    print(f"Humidity: {weather_data.humidity}%")

    # Request structured output for mathematical reasoning
    reply = await agent.chat(
        "how can I solve 8x + 7 = -23", 
        session, 
        output_type=MathReasoning
    )
    for index, step in enumerate(reply.steps):
        print(f"Step {index + 1}: {step.explanation} => Output: {step.output}")
    print("Final Answer:", reply.final_answer)

asyncio.run(get_structured_response())
```

### 🤖 Agent as Tool Pattern

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
    
    writing_agent = Agent(
        name="writing_specialist", 
        system_prompt="Professional writer. Create engaging content.",
        model="gpt-4.1-mini"
    )
    
    # Convert agents to tools
    message_db = MessageDB()
    research_tool = researcher_agent.as_tool(
        name="researcher",
        description="Research topics and provide detailed analysis",
        message_db=message_db
    )
    
    writing_tool = writing_agent.as_tool(
        name="content_writer",
        description="Write and edit content",
        message_db=message_db
    )
    
    # Main coordinator agent with specialist tools
    coordinator = Agent(
        name="coordinator",
        tools=[research_tool, writing_tool],
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

## 🌐 HTTP Agent Server

xAgent provides a standalone HTTP server that exposes the Agent functionality through REST API endpoints. This allows integration with other systems and services through simple HTTP calls.

### 🚀 Starting the HTTP Server

```bash
# Start with default config
python xagent/core/server.py --config config/agent.yaml

# Server will start on http://localhost:8010 by default
```

### ⚙️ Configuration

The HTTP server is configured through a YAML file (e.g., `config/agent.yaml`):

```yaml
agent:
  name: "Agent"
  system_prompt: |
    You are a helpful assistant. Your task is to assist users with their queries and tasks.
  model: "gpt-4.1-mini"
  mcp_servers:
    - "http://localhost:8001/mcp/"
  tools:
    - "web_search"
  use_local_session: true

server:
  host: "0.0.0.0"
  port: 8010
  debug: true
```

### 📡 API Endpoints

#### POST `/chat`

Main chat endpoint for interacting with the AI agent.

**Request Body:**
```json
{
  "user_id": "string",      
  "session_id": "string",   
  "user_message": "string", 
  "image_source": "string"  
}
```

image_source: Image URL or base64 encoded image (Optional)

**Response:**
```json
{
  "reply": "string"
}
```

### 💡 Usage Examples

#### Basic Chat Request

```bash
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user123",
    "session_id": "session456",
    "user_message": "Hello, how are you?"
  }'
```

#### Chat with Image

```bash
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user123",
    "session_id": "session456", 
    "user_message": "What do you see in this image?",
    "image_source": "https://example.com/image.jpg"
  }'
```

#### Python Client Example

```python
import requests
import json

def chat_with_agent(user_message, user_id="test", session_id="test", image_source=None):
    url = "http://localhost:8010/chat"
    
    payload = {
        "user_id": user_id,
        "session_id": session_id,
        "user_message": user_message
    }
    
    if image_source:
        payload["image_source"] = image_source
    
    response = requests.post(url, json=payload)
    
    if response.status_code == 200:
        return response.json()["reply"]
    else:
        return f"Error: {response.status_code}"

# Usage
reply = chat_with_agent("你是谁")
print(reply)

# Continue conversation with context
reply = chat_with_agent("我的名字是张三", session_id="session456")
print(reply)

reply = chat_with_agent("我的名字是什么？", session_id="session456")
print(reply)  # Will remember the name from previous message
```

### 🔧 Features

| Feature | Description |
|---------|-------------|
| **Configuration-Driven** | Easy setup through YAML config files |
| **Session Management** | Automatic conversation context preservation |
| **Multi-Modal Support** | Text and image processing capabilities |
| **Tool Integration** | Configurable tool ecosystem through YAML |
| **MCP Protocol** | Dynamic tool loading from MCP servers |
| **RESTful API** | Standard HTTP/JSON interface |
| **Stateless Design** | Each request is independent with session context |

### 🎯 Use Cases

- **Microservice Integration** - Embed AI capabilities in existing systems
- **API Gateway** - Centralized AI service for multiple applications  
- **Mobile App Backend** - Provide AI chat functionality to mobile apps
- **Webhook Processing** - Process incoming webhooks with AI analysis
- **Batch Processing** - Process multiple requests programmatically
- **Third-party Integrations** - Connect with external platforms and services


## 🔧 Development Guide

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

###  📋 Tool Development Guidelines

| Use Case | Tool Type | Example |
|----------|-----------|---------|
| **CPU-bound** | Sync functions | Math calculations, data processing |
| **I/O-bound** | Async functions | API calls, database queries |
| **Simple operations** | Sync functions | String manipulation, file operations |
| **Network requests** | Async functions | HTTP requests, WebSocket connections |

> **⚠️ Note**: Recursive functions are not supported as tools due to potential stack overflow issues in async environments.

###  🔄 Automatic Conversion

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

`function_tool` decorator can pass `name` and `description` to override defaults

## 🤖 API Reference

### Core Classes

🤖 Agent

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


💬 Session

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

**Features:**
- Automatic fallback to in-memory storage if no MessageDB provided
- Redis-backed persistence for production use
- Thread-safe operations
- Efficient message batching


🗄️ MessageDB

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
