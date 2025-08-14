# xAgent - Multi-Modal AI Agent System

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)](https://fastapi.tiangolo.com/)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.28+-red.svg)](https://streamlit.io/)
[![Redis](https://img.shields.io/badge/Redis-7.0+-red.svg)](https://redis.io/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **🚀 A powerful multi-modal AI Agent system with real-time streaming responses**

xAgent provides an end-to-end AI assistant experience—covering text and image processing, concurrent tool execution, an intuitive HTTP server, a web interface, and a real-time streaming CLI—built with FastAPI, Streamlit, and Redis to scale in production.

## 📋 Table of Contents

- [🚀 Quick Start](#-quick-start)
- [🚀 Installation & Setup](#-installation--setup)
- [🌐 HTTP Agent Server](#-http-agent-server)
- [🌐 Web Interface](#-web-interface)
- [💻 Command Line Interface (CLI)](#-command-line-interface-cli)
- [🤖 Advanced Usage: Agent Class](#-advanced-usage-agent-class)
- [🏗️ Architecture](#%EF%B8%8F-architecture)
- [🤖 API Reference](#-api-reference)
- [📊 Monitoring & Observability](#-monitoring--observability)
- [🤝 Contributing](#-contributing)
- [📄 License](#-license)


## 🚀 Quick Start

To quickly start using xAgent, install the package and set your OpenAI API key. Then you can run the `CLI` or `HTTP server` to interact with your AI agent.

```bash
# Install xAgent
pip install myxagent

# Set your OpenAI API key
export OPENAI_API_KEY=your_openai_api_key

# Start the CLI with default configuration
xagent-cli

# Or start the HTTP server with default configuration
xagent-server

# Start the Streamlit web interface (Optional)
xagent-web

# Start the visual configuration interface (NEW!)
xagent-config
```

If start a http server, you can interact with the agent using the following command:

```bash
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user123",
    "session_id": "session456",
    "user_message": "Who are you?",
    "stream": false
  }'
```

More about cli and http server usage, please refer to the [CLI](#-command-line-interface-cli) and [HTTP Agent Server](#-http-agent-server) sections.


## 🚀 Installation & Setup

### Prerequisites

| Requirement | Version | Purpose |
|-------------|---------|---------|
| **Python** | 3.12+ | Core runtime |
| **OpenAI API Key** | - | AI model access |

### Install via pip

```bash
pip install myxagent

# use official PyPI
pip install myxagent -i https://pypi.org/simple

# or use Aliyun mirror for faster download in China
pip install myxagent -i https://mirrors.aliyun.com/pypi/simple
```


### Environment Configuration

Create a `.env` file in your project directory and add the following variables:

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


## 🌐 HTTP Agent Server

The simplest way to use xAgent is through the HTTP server. Just create a config file and start serving!

### 1. Create Agent Configuration

Create `agent_config.yaml`:

```yaml
agent:
  name: "MyAgent"
  system_prompt: |
    You are a helpful assistant. Your task is to assist users with their queries and tasks.
  model: "gpt-4.1-mini"

  capabilities:
    tools:
      - "web_search"  # Built-in web search
      - "draw_image"  # Built-in image generation (need set AWS credentials in .env)
      - "calculate_square"  # Custom tool from my_toolkit
      
server:
  host: "0.0.0.0"
  port: 8010
```

If you want to use MCP (Model Context Protocol) for dynamic tool loading, you can also add `mcp_servers` in agent configuration

Example for how to start a MCP server can be found in `toolkit/mcp_server.py`:

```yaml
agent:
  ...
  capabilities:
    mcp_servers:
      - "http://localhost:8001/mcp/"  # MCP server URL
  ...
```

If you use Redis, you can set `use_local_session` to `false` (make sure to configure `REDIS_URL` in the `.env` file). This way, when deploying multiple services, the conversation can remain consistent even if requests are routed to different service instances.

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

# Agent will automatically discover these tools,you can choose which to load in agent config
TOOLKIT_REGISTRY = {
    "calculate_square": calculate_square,
    "fetch_weather": fetch_weather
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
async def fetch_weather(city: str) -> str:
    """Fetch weather data for a city (dummy implementation)."""
    return f"The weather in {city} is sunny with a high of 25°C."

```

You can override the default tool name and description using the `function_tool` decorator:

```python
@function_tool(name="custom_square", description="Calculate the square of a number")
def calculate_square(n: int) -> int:
    return n * n
```

### 3. Start the Server

```bash
# Start the HTTP Agent Server with default configuration
xagent-server

# With custom configuration and toolkit
xagent-server --config agent_config.yaml --toolkit_path my_toolkit

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

### 5. Advanced Configuration （Hierarchical Multi-Agent System）

xAgent supports sophisticated multi-agent architectures and advanced configuration options for complex use cases. (agent as tool pattern)

#### Multi-Agent System with Sub-Agents

Create a hierarchical agent system where a coordinator agent delegates tasks to specialized sub-agents:

Main Agent Configuration (`coordinator_agent.yaml`):
```yaml
agent:
  name: "Agent"
  system_prompt: |
    You are Orion, a helpful, concise, and accurate assistant who coordinates specialized agents.  
    - Always answer clearly and directly.  
    - When the task requires research, delegate it to the `research_agent`.  
    - When the task requires writing, editing, or creative content generation, delegate it to the `write_agent`.  
    - Keep responses focused, relevant, and free of unnecessary filler.  
    - If more details or clarifications are needed, ask before proceeding.  
    - Maintain a friendly and professional tone while ensuring efficiency in task delegation.  
    - Your goal is to act as the central hub, ensuring each request is handled by the most capable resource.  
  model: "gpt-4.1"

  capabilities:
    tools:
      - "char_count" # custom tool for character counting
    mcp_servers:
      - "http://localhost:8001/mcp/"
  
  sub_agents:
    - name: "research_agent"
      description: "Research-focused agent for information gathering and analysis"
      server_url: "http://localhost:8011"
    - name: "write_agent"
      description: "Expert agent for writing tasks, including content creation and editing"
      server_url: "http://localhost:8012"

  use_local_session: true

server:
  host: "0.0.0.0"
  port: 8010
```

Research Specialist (`research_agent.yaml`):
```yaml
agent:
  name: "Research Agent"
  system_prompt: |
    You are Tom, a research specialist.  
    Your role is to gather accurate and up-to-date information using web search, evaluate sources critically, and deliver well-organized, insightful findings.  
    - Always verify the credibility of your sources.  
    - Present information in a clear, concise, and structured format.  
    - Highlight key facts, trends, and supporting evidence.  
    - When applicable, compare multiple sources to ensure accuracy.  
    - If information is uncertain or unavailable, state this transparently.  
  model: "gpt-4.1-mini"

  capabilities:
    tools:
      - "web_search" # built-in web search tool
    mcp_servers:
      - "http://localhost:8002/mcp/"
  
  use_local_session: true

server:
  host: "0.0.0.0"
  port: 8011
```

Writing Specialist (`writing_agent.yaml`):
```yaml
agent:
  name: "Writing Agent"
  system_prompt: |
    You are Alice, a professional writer.  
    Your role is to craft clear, engaging, and well-structured content tailored to the intended audience and purpose.  
    - Adapt tone, style, and format to match the context.  
    - Use vivid language and strong storytelling techniques when appropriate.  
    - Ensure clarity, coherence, and grammatical accuracy.  
    - Organize ideas logically and maintain a smooth flow.  
    - Revise and refine content for maximum impact and readability.  
  model: "gpt-4.1-mini"

  capabilities:
    tools: []
    mcp_servers:
      - "http://localhost:8003/mcp/"
  
  use_local_session: true

server:
  host: "0.0.0.0"
  port: 8012
```

#### Starting Multi-Agent System

```bash
# Start sub-agents first
xagent-server --config research_agent.yaml > logs/research.log 2>&1 &
xagent-server --config writing_agent.yaml > logs/writing.log 2>&1 &

# Start coordinator agent
xagent-server --config coordinator_agent.yaml --toolkit_path my_toolkit > logs/coordinator.log 2>&1 &

# Verify all agents are running
curl http://localhost:8010/health
curl http://localhost:8011/health
curl http://localhost:8012/health

# Now you can chat with the coordinator agent through its API
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user123",
    "session_id": "session456",
    "user_message": "Research the latest advancements in AI and write a summary."
  }'
```

You can create sub-agents with any depth you want, forming a hierarchical tree structure of agents. Just make sure there are no circular references, and start the agents in a bottom-up order.

### 6. Advanced Configuration （Structured Output with Pydantic Models）

xagent now supports defining structured output schemas directly in the YAML configuration file. This feature allows you to specify the expected output format using Pydantic models, ensuring type safety and easy parsing of the agent's responses.

#### Structured Output Configuration

In your YAML configuration file, you can define output_schema like this:

```yaml
agent:
  name: "YourAgent"
  system_prompt: "Your system prompt here"
  model: "gpt-4o-mini"
  
  output_schema:
    class_name: "YourModelName"  # Pydantic model class name
    fields:
      field_name:
        type: "field_type"        # Field type (str, int, float, bool, list, dict)
        description: "description"    # Field description
      list_field:
        type: "list"
        items: "str"              # List item type (required for list fields)
        description: "A list of strings"
```

#### Supported Field Types

- `str` - string type
- `int` - integer type
- `float` - floating-point type
- `bool` - boolean type
- `list` - list type
- `dict` - dictionary type


Important Notes:
- When using the `list` type, you must specify the element type through the items field.
- This is to comply with OpenAI JSON Schema validation requirements.
- items supports any basic type: `str`, `int`, `float`, `bool`, etc.

Example of a content generation model (with a list field):

```yaml
agent:
  name: "ContentAgent"
  system_prompt: |
    You are a content generation assistant. 
    Generate structured content with images.
  model: "gpt-4o-mini"
  capabilities:
    tools:
      - "web_search"
      - "draw_image"
  
  output_schema:
    class_name: "ContentReport"
    fields:
      title:
        type: "str"
        description: "The title of the content report"
      content:
        type: "str"
        description: "The main content of the report"
      images:
        type: "list"
        items: "str"  # List of image URLs with string type
        description: "List of image URLs related to the content"
      tags:
        type: "list"
        items: "str"
        description: "List of relevant tags"
```

The above configuration will automatically generate the following Pydantic model:

```python
from typing import List
from pydantic import BaseModel, Field

class ContentReport(BaseModel):
    title: str = Field(description="The title of the content report")
    content: str = Field(description="The main content of the report")
    images: List[str] = Field(description="List of image URLs related to the content")
    tags: List[str] = Field(description="List of relevant tags")
```

An agent started this way will automatically create a Pydantic model based on the specified output_schema and return structured output during conversations.

## 🌐 Web Interface

xAgent provides multiple web interfaces for different use cases:

### 1. Chat Interface
User-friendly Streamlit chat interface for interactive conversations with your AI agent.

```bash
# Start the chat interface with default settings
xagent-web

# With custom agent server URL
xagent-web --agent-server http://localhost:8010

# With custom host and port
xagent-web --host 0.0.0.0 --port 8501 --agent-server http://localhost:8010
```

### 2. Configuration Interface (NEW!)
Visual configuration interface for creating and managing xAgent servers without writing YAML files.

```bash
# Start the configuration interface
xagent-config

# With custom host and port
xagent-config --host 0.0.0.0 --port 8502
```

### Web Interface Options

| Interface | Default Port | Description |
|-----------|--------------|-------------|
| Chat Interface | 8501 | Interactive conversation with agents |
| Config Interface | 8502 | Visual agent configuration and management |

### Complete Web Setup Example

```bash
# Terminal 1: Start the agent server using config UI
xagent-config
# Create and start your agent through the web interface

# Terminal 2: Start the chat interface  
xagent-web --agent-server http://localhost:8010

# Access:
# - Configuration UI: http://localhost:8502
# - Chat Interface: http://localhost:8501
```

## 💻 Command Line Interface (CLI)

xAgent provides a powerful command-line interface for quick interactions and testing. The CLI supports both single-question mode and interactive chat sessions, with **real-time streaming responses** for a smooth conversational experience.

Note: do not support sub-agents in CLI mode currently.

### Quick Start

```bash
# Interactive chat mode with streaming (default)
xagent-cli

# Use custom configuration
xagent-cli chat --config my_config.yaml --toolkit_path my_toolkit --user_id developer --session_id session123 --verbose

# Ask a single question (non-streaming)
xagent-cli ask "What is the capital of France?"

```

### Interactive Chat Mode

Start a continuous conversation with the agent with **streaming enabled by default**:

```bash
$ xagent-cli chat
🤖 Welcome to xAgent CLI!
Agent: Agent
Model: gpt-4.1-mini
Tools: 3 loaded
Session: cli_session_abc123
Streaming: Enabled
Type 'exit', 'quit', or 'bye' to end the session.
Type 'clear' to clear the session history.
Type 'stream on/off' to toggle streaming mode.
Type 'help' for available commands.
--------------------------------------------------

👤 You: Hello, how are you?
🤖 Agent: Hello! I'm doing well, thank you for asking...
[Response streams in real-time]

👤 You: help
📋 Available commands:
  exit, quit, bye  - Exit the chat session
  clear           - Clear session history
  stream on/off   - Toggle streaming mode
  help            - Show this help message

👤 You: exit
👋 Goodbye!
```

### CLI Commands Reference

| Command | Description | Example |
|---------|-------------|---------|
| `xagent-cli` | Start interactive chat with streaming (default) | `xagent-cli` |
| `xagent-cli chat` | Start interactive chat explicitly | `xagent-cli chat --config my_config.yaml` |
| `xagent-cli ask <message>` | Ask single question (non-streaming) | `xagent-cli ask "Hello world"` |

### CLI Options

| Option | Description | Default |
|--------|-------------|---------|
| `--config` | Configuration file path | `config/agent.yaml` |
| `--toolkit_path` | Custom toolkit directory | `toolkit` |
| `--user_id` | User identifier | Auto-generated |
| `--session_id` | Session identifier | Auto-generated |
| `--verbose`, `-v` | Enable verbose logging | `False` |

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

With Pydantic structured outputs, you can:
- Parse and validate an agent’s response into typed data
- Easily extract specific fields
- Ensure the response matches the expected format
- Guarantee type safety in your application
- Reliably chain multi-step tasks using structured data

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
    
    agent = Agent(model="gpt-4.1-mini", 
                  tools=[web_search], 
                  output_type=WeatherReport) # You can set a default output type here or leave it None
    
    session = Session(user_id="user123")
    
    # Request structured output for weather
    weather_data = await agent.chat(
        "what's the weather like in Hangzhou?",
        session
    )
    
    print(f"Location: {weather_data.location}")
    print(f"Temperature: {weather_data.temperature}°F")
    print(f"Condition: {weather_data.condition}")
    print(f"Humidity: {weather_data.humidity}%")


    # Request structured output for mathematical reasoning (overrides output_type)
    reply = await agent.chat("how can I solve 8x + 7 = -23", session, output_type=MathReasoning) # Override output_type for this call
    for index, step in enumerate(reply.steps):
        print(f"Step {index + 1}: {step.explanation} => Output: {step.output}")
    print("Final Answer:", reply.final_answer)

if __name__ == "__main__":
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

## 🏗️ Architecture

**Modern Design for High Performance**

```
xAgent/
├── 🤖 xagent/                # Core async agent framework
│   ├── __init__.py           # Package initialization and exports
│   ├── __version__.py        # Version information
│   ├── core/                 # Agent and session management
│   │   ├── __init__.py       # Core exports (Agent, Session, HTTPAgentServer)
│   │   ├── agent.py          # Main Agent class with chat
│   │   ├── session.py        # Session management with operations
│   │   ├── server.py         # Standalone HTTP Agent Server
│   │   ├── cli.py            # Command line interface
│   │   └── base.py           # Base classes and utilities
│   ├── db/                   # Database layer (Redis)
│   │   ├── __init__.py       # Database exports
│   │   └── message.py        # Message persistence
│   ├── schemas/              # Data models and types (Pydantic)
│   │   ├── __init__.py       # Schema exports
│   │   └── message.py        # Message and ToolCall models
│   ├── tools/                # Tool ecosystem
│   │   ├── __init__.py       # Tool registry (web_search, draw_image)
│   │   ├── openai_tool.py    # OpenAI tool integrations
│   │   └── mcp_demo/         # MCP demo server and client
│   ├── utils/                # Utility functions
│   ├── multi/                # Multi-agent support
│   │   ├── __init__.py       # Multi-agent exports
│   │   ├── swarm.py          # Agent swarm coordination
│   │   └── workflow.py       # Workflow management
│   └── frontend/             # Web interface components
│       ├── app.py            # Streamlit chat application
│       └── launcher.py       # Web interface launcher
├── 🛠️ toolkit/               # Custom tool ecosystem
│   ├── __init__.py           # Toolkit registry
│   ├── tools.py              # Custom tools (char_count)
│   ├── mcp_server.py         # Main MCP server
├── ⚙️ config/                # Configuration files
│   ├── agent.yaml            # Agent server configuration
│   ├── structure_examples/   # Structured output examples
│   └── sub_agents_example/   # Sub-agent configuration examples
├── 📝 examples/              # Usage examples and demos
├── 🧪 tests/                 # Comprehensive test suite
├── 📁 logs/                  # Log files
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
    mcp_servers: Optional[str | list] = None,
    sub_agents: Optional[List[Union[tuple[str, str, str], 'Agent']]] = None
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
- `sub_agents`: List of sub-agent configurations (name, description, server URL)

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
