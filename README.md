# xAgent - Multi-Modal Conversational AI System

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)](https://fastapi.tiangolo.com/)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.28+-red.svg)](https://streamlit.io/)
[![Redis](https://img.shields.io/badge/Redis-7.0+-red.svg)](https://redis.io/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

xAgent is a powerful multi-modal conversational AI system that supports text, image interactions, and vocabulary learning. Built with **async-first architecture**, FastAPI backend, Streamlit frontend, and Redis for persistence, it provides a complete AI assistant experience with intelligent vocabulary management and concurrent tool execution.

## ✨ Features

### 🤖 **Multi-Modal AI Chat**
- **Text Conversations**: Powered by OpenAI GPT models (GPT-4.1, GPT-4.1-mini, GPT-4o)
- **Image Processing**: Upload and analyze images with AI
- **Session Management**: Persistent conversation history with Redis and async operations
- **Tool Integration**: Extensible tool system with automatic sync-to-async conversion
- **MCP Support**: Model Context Protocol integration for dynamic tool loading
- **Concurrent Execution**: Parallel tool execution for improved performance

### 📚 **Intelligent Vocabulary Learning**
- **Smart Word Lookup**: AI-powered word definitions with difficulty levels
- **Personalized Vocabulary**: User-specific word learning and tracking
- **Familiarity System**: Track learning progress with smart review recommendations
- **Multi-Level Learning**: Beginner, Intermediate, and Advanced word explanations

### 🔧 **Developer-Friendly Architecture**
- **Async-First Design**: Built for high-performance concurrent operations
- **Modular Design**: Clean separation of concerns with pluggable components
- **MCP Protocol**: Model Context Protocol server for tool integration
- **Comprehensive Testing**: Full test coverage with pytest
- **Observability**: Built-in logging and monitoring with Langfuse
- **Type Safety**: Full type hints with Pydantic models

## 🏗️ Architecture

```
xAgent/ (Async-First Architecture)
├── api/                    # FastAPI backend services
│   ├── main.py            # API server entry point
│   ├── health.py          # Health check endpoints
│   └── schemas/           # API data models
├── frontend/              # Streamlit web interface
│   └── chat_app.py        # Main chat application
├── xagent/                # Core async agent framework
│   ├── core/              # Agent and session management (async)
│   │   ├── agent.py       # Main Agent class with async chat
│   │   └── session.py     # Session management with async operations
│   ├── db/                # Async database layer (Redis)
│   │   └── message.py     # Async message persistence
│   ├── schemas/           # Data models and types (Pydantic)
│   ├── tools/             # Async tool ecosystem
│   │   ├── mcp_server.py      # MCP protocol server
│   │   ├── openai_tool.py     # OpenAI tool integrations
│   │   └── vocabulary/        # Vocabulary learning system
│   └── utils/             # Async utility functions
│       ├── tool_decorator.py  # Async tool decorators
│       └── mcp_convertor.py   # MCP async client
├── examples/              # Async usage examples
│   └── run_agent.py       # Complete async examples
└── tests/                 # Async test suite
    └── test_*.py          # Comprehensive async tests
```

### Key Async Components:

- **Agent**: Core async conversation handler with concurrent tool execution
- **Session**: Async message history management with Redis integration  
- **MessageDB**: Async Redis operations for scalable persistence
- **Tools**: Automatic sync-to-async conversion for optimal performance
- **Tool Decorator**: Smart async wrapper for both sync and async functions
- **MCP Integration**: Async Model Context Protocol support

## 🚀 Quick Start

### Prerequisites

- Python 3.12+
- Redis Server
- OpenAI API Key
- Understanding of Python async/await patterns

### Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/ZJCODE/xAgent.git
   cd xAgent
   ```

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt

   or 

   pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
   ```

3. **Environment Setup**
   ```bash
   # Create .env file
   cp .env.example .env
   
   # Configure environment variables
   OPENAI_API_KEY=your_openai_api_key

   REDIS_URL=your_redis_url_with_password # Optional

   LANGFUSE_SECRET_KEY=your_langfuse_key  # Optional
   LANGFUSE_PUBLIC_KEY=your_langfuse_public_key  # Optional
   LANGFUSE_HOST=https://cloud.langfuse.com  # Optional
   
   AWS_ACCESS_KEY_ID=your_aws_access_key_id # Optional
   AWS_SECRET_ACCESS_KEY=your_aws_secret_access_key # Optional
   AWS_REGION=us-east-1 # Optional
   BUCKET_NAME=your_bucket_name # Optional
   ```

### Running the Application

#### Option 1: Quick Start (All Services)
```bash
chmod +x run.sh
./run.sh
```

#### Option 2: Manual Start

1. **Start API Server**
   ```bash
   uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
   ```

2. **Start MCP Server**
   ```bash
   python tools/mcp_server.py
   ```

3. **Start Frontend**
   ```bash
   streamlit run frontend/chat_app.py --server.port 8501
   ```

### Access the Application

- **Chat Interface**: http://localhost:8501
- **API Documentation**: http://localhost:8000/docs
- **Health Check**: http://localhost:8000/health

## ⚡ Async Best Practices

### 1. **Always Use Async Context**
```python
import asyncio

# ✅ Correct: Run in async context
async def main():
    agent = Agent()
    session = Session(user_id="user123")
    response = await agent.chat("Hello", session)
    print(response)

asyncio.run(main())

# ❌ Incorrect: Don't use sync context
# response = agent.chat("Hello", session)  # This will fail
```

### 2. **Flexible Tool Development** (Sync or Async)
```python
# Both sync and async tools work seamlessly
@function_tool()
def cpu_intensive_task(data: str) -> str:
    # Sync function for CPU-bound work - runs in thread pool
    import time
    time.sleep(1)  # Simulate CPU work
    return f"Processed: {data}"

@function_tool()
async def io_intensive_task(url: str) -> str:
    # Async function for I/O-bound work - runs directly
    import httpx
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        return response.text[:100]

# Agent automatically handles both types concurrently
agent = Agent(tools=[cpu_intensive_task, io_intensive_task])
```

### 3. **Session Management**
```python
# ✅ Reuse session for conversation continuity
async def conversation_example():
    agent = Agent()
    session = Session(user_id="user123", session_id="chat001")
    
    # First message
    await agent.chat("My name is Alice", session)
    
    # Context is preserved automatically
    response = await agent.chat("What's my name?", session)
    # Response: "Your name is Alice"
```

### 4. **Error Handling in Async Context**
```python
async def robust_chat():
    agent = Agent()
    session = Session(user_id="user123")
    
    try:
        response = await agent.chat("Complex query", session)
        print(response)
    except Exception as e:
        print(f"Chat failed: {e}")
        # Handle gracefully
```

### 5. **Memory Management for Long Conversations**
```python
async def long_conversation():
    agent = Agent()
    session = Session(user_id="user123")
    
    # Control message history to prevent memory issues
    response = await agent.chat(
        "Tell me about AI", 
        session,
        history_count=10  # Only use last 10 messages for context
    )
```

## 🔧 Tool Development Guide

### Understanding Automatic Async Conversion

xAgent's `@function_tool()` decorator automatically converts sync functions to async, making tool development flexible and intuitive:

```python
from xagent.utils.tool_decorator import function_tool
import time
import asyncio
import httpx

# ✅ Sync function - automatically wrapped for thread-pool execution
@function_tool()
def cpu_heavy_task(n: int) -> int:
    """Calculate sum of squares (CPU-intensive)."""
    time.sleep(0.1)  # Simulate heavy computation
    return sum(i**2 for i in range(n))

# ✅ Async function - used directly on event loop  
@function_tool()
async def network_request(url: str) -> str:
    """Fetch data from URL (I/O-intensive)."""
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        return response.text[:100]

# ✅ Simple sync function - no need to make it async
@function_tool()
def simple_math(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b  # No async needed for simple operations
```

### When to Use Sync vs Async

**Use Sync Functions For:**
- Mathematical calculations  
- Data transformations
- File operations (small files)
- Simple string/data processing
- CPU-bound operations

**Use Async Functions For:**
- HTTP requests
- Database queries  
- File I/O (large files)
- External API calls
- Network operations

### Performance Characteristics

```python
# Concurrent execution example
async def demo_concurrent_tools():
    agent = Agent(tools=[
        cpu_heavy_task,    # Runs in thread pool
        network_request,   # Runs on event loop  
        simple_math        # Runs in thread pool
    ])
    
    session = Session(user_id="demo")
    
    # All tools execute concurrently when called by agent
    # - sync tools don't block the event loop
    # - async tools run directly for optimal I/O performance
    # - total execution time = max(individual_times), not sum
    
    response = await agent.chat(
        "Calculate sum of squares for 1000, fetch https://httpbin.org/json, and add 5+3",
        session
    )
```

## 💡 Usage Examples

### Basic Async Chat
```python
import asyncio
from xagent.core import Agent, Session
from xagent.db import MessageDB
from xagent.tools.openai_tool import web_search

async def main():
    # Create agent with async-aware architecture
    agent = Agent(
        name="my_assistant",
        system_prompt="You are a helpful AI assistant.",
        model="gpt-4.1-mini"  # Using latest model,
        tools=[web_search]  # Add web search tool
    )

    # Create session for conversation management
    session = Session(
        session_id="session456",
    )

    # Async chat interaction
    response = await agent.chat("Hello, how are you?", session)
    print(response)

    # Continue conversation with context
    response = await agent.chat("What's the weather like in Hangzhou?", session)
    print(response)

# Run the async function
asyncio.run(main())
```

### Advanced Chat with Redis Persistence
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

### Custom Tools (Sync and Async)
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
    import time
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

### Structured Output with Pydantic
```python
import asyncio
from pydantic import BaseModel
from xagent.core import Agent, Session
from xagent.tools.openai_tool import web_search

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
        "Generate weather data for New York",
        session,
        output_type=WeatherReport
    )
    
    print(f"Location: {weather_data.location}")
    print(f"Temperature: {weather_data.temperature}°F")
    print(f"Condition: {weather_data.condition}")

asyncio.run(get_structured_response())
```

### Agent as Tool Pattern
```python
import asyncio
from xagent.core import Agent, Session
from xagent.db import MessageDB

async def agent_as_tool_example():
    # Create specialized agents
    math_agent = Agent(
        name="math_specialist",
        system_prompt="You are a mathematics expert. Solve problems step by step.",
        model="gpt-4.1-mini"
    )
    
    writing_agent = Agent(
        name="writing_specialist", 
        system_prompt="You are a professional writer. Create engaging content.",
        model="gpt-4.1-mini"
    )
    
    # Convert agents to tools
    message_db = MessageDB()
    math_tool = math_agent.as_tool(
        name="math_solver",
        description="Solve mathematical problems",
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
        tools=[math_tool, writing_tool],
        system_prompt="You coordinate between specialists to solve complex tasks.",
        model="gpt-4.1-mini"
    )
    
    session = Session(user_id="user123")
    
    # Complex multi-step task
    response = await coordinator.chat(
        "Calculate the compound interest for $1000 at 5% for 10 years, then write a brief explanation",
        session
    )
    print(response)

asyncio.run(agent_as_tool_example())
```


## 📊 Monitoring & Observability

xAgent includes built-in observability features:

- **Langfuse Integration**: Track AI interactions and performance
- **Comprehensive Logging**: Structured logging throughout the system
- **Health Checks**: API health monitoring endpoints
- **Performance Metrics**: Tool execution time and success rates

## 🔧 Development

### Project Structure

- **`api/`**: FastAPI backend with health checks and API endpoints.
- **`frontend/`**: Streamlit-based chat interface with image upload support.
- **`xagent/`**: The core agent framework, containing:
    - **`core/`**: Core agent logic and session management.
    - **`db/`**: Redis-based persistence layer for conversations.
    - **`tools/`**: Extensible tool ecosystem, including the vocabulary learning system.
    - **`schemas/`**: Data models and types used across the framework.
    - **`utils/`**: Shared utility functions.
- **`examples/`**: Example scripts demonstrating how to use the xAgent framework.
- **`tests/`**: Comprehensive test suite for the project.


### Adding New Tools

1. Create a tool function (sync or async) with the `@function_tool()` decorator
2. Add to the agent's tool list  
3. Test with the provided test framework

```python
import asyncio
import time
from xagent.utils.tool_decorator import function_tool

# Sync tool - perfect for CPU-bound operations
@function_tool()
def my_sync_tool(input_text: str) -> str:
    """Process text synchronously (runs in thread pool)."""
    # Simulate CPU-intensive work
    time.sleep(0.1)
    return f"Sync processed: {input_text}"

# Async tool - ideal for I/O-bound operations  
@function_tool()
async def my_async_tool(input_text: str) -> str:
    """Process text asynchronously."""
    # Simulate async I/O operation
    await asyncio.sleep(0.1)
    return f"Async processed: {input_text}"

# Use with agent
async def main():
    agent = Agent(tools=[my_sync_tool, my_async_tool])
    session = Session(user_id="user123")
    
    response = await agent.chat("Use both tools to process 'hello world'", session)
    print(response)

asyncio.run(main())
```

**Tool Development Guidelines:**
- Use **sync functions** for CPU-bound operations (math, data processing, file operations)
- Use **async functions** for I/O-bound operations (API calls, database queries, network requests)
- Both types execute concurrently when called by the agent
- Sync tools automatically run in thread pools to avoid blocking
- **Note**: Recursive functions are not supported as tools due to potential stack overflow issues in async environments

### MCP Protocol Integration

xAgent supports the Model Context Protocol for tool integration with full async support:

```python
import asyncio
from xagent.core import Agent, Session
from xagent.utils.mcp_convertor import MCPTool

async def mcp_integration_example():
    # Connect to MCP server
    mcp_tool = MCPTool("http://localhost:8001/mcp/")
    tools = await mcp_tool.get_openai_tools()

    # Create agent with MCP tools
    agent = Agent(
        tools=tools,
        mcp_servers=["http://localhost:8001/mcp/"],  # Auto-refresh MCP tools
        model="gpt-4.1-mini"
    )
    
    session = Session(user_id="user123")
    
    # Use MCP tools automatically
    response = await agent.chat("Use the available MCP tools to help me", session)
    print(response)

asyncio.run(mcp_integration_example())
```

## 📝 API Reference

### Core Classes

#### `Agent`
Main AI agent class for handling async conversations and tool execution.

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
- `as_tool(name, description, message_db) -> Callable`: Convert agent to async tool

**Parameters:**
- `name`: Agent identifier (default: "default_agent")
- `system_prompt`: Instructions for the agent behavior
- `model`: OpenAI model to use (default: "gpt-4.1-mini")
- `client`: Custom AsyncOpenAI client instance
- `tools`: List of async function tools
- `mcp_servers`: MCP server URLs for dynamic tool loading

#### `Session`
Manages conversation history and persistence with async operations.

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
- Thread-safe async operations
- Efficient message batching

#### `MessageDB`
Redis-backed async message persistence layer.

```python
# Initialize with environment variables or defaults
message_db = MessageDB()

# Usage with session
session = Session(
    user_id="user123",
    message_db=message_db
)
```

### Important Async Considerations

1. **Tool functions can be sync or async** (automatic conversion):
   ```python
   # ✅ Sync function - automatically converted to async
   @function_tool()
   def simple_calculator(a: int, b: int) -> int:
       """Add two numbers together."""
       return a + b
   
   # ✅ Async function - used directly
   @function_tool()
   async def api_call(query: str) -> str:
       """Make an API call."""
       async with httpx.AsyncClient() as client:
           response = await client.get(f"https://api.example.com/{query}")
           return response.text
   ```

2. **Always use `await` with agent interactions**:
   ```python
   response = await agent.chat(message, session)
   ```

3. **Run in async context**:
   ```python
   import asyncio
   
   async def main():
       # Your async code here
       pass
   
   asyncio.run(main())
   ```

4. **Automatic async conversion benefits**:
   - Sync functions are wrapped in `loop.run_in_executor()` for thread-pool execution
   - CPU-bound sync functions don't block the event loop
   - Async functions run directly for I/O-bound operations
   - All tools execute concurrently using `asyncio.gather()`
   - Maintains conversation context consistency

## 🤝 Contributing

We welcome contributions! Please follow these steps:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

### Development Guidelines

- Follow PEP 8 coding standards
- Add tests for new features
- Update documentation as needed
- Use type hints throughout
- Follow conventional commit messages

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- [OpenAI](https://openai.com/) for the GPT models
- [FastAPI](https://fastapi.tiangolo.com/) for the robust API framework
- [Streamlit](https://streamlit.io/) for the intuitive web interface
- [Redis](https://redis.io/) for high-performance data storage
- [Langfuse](https://langfuse.com/) for observability and monitoring

## 📞 Support

- **Issues**: [GitHub Issues](https://github.com/yourusername/xAgent/issues)
- **Discussions**: [GitHub Discussions](https://github.com/yourusername/xAgent/discussions)
- **Email**: support@xagent.dev

---

**xAgent** - Empowering conversations with AI 🚀
