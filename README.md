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

# Start the CLI with default configuration （interactive mode）
xagent-cli

# Or start the HTTP server with default configuration （for development）
xagent-server

```

If start a http server, you can interact with the agent using the following command:

```bash
# Basic chat request
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user123",
    "session_id": "session456",
    "user_message": "Who are you?",
    "stream": false
  }'
```

or start a Streamlit web interface to interact with the agent server:

```bash
xagent-web
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

# upgrade to the latest version
pip install --upgrade myxagent

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

If you use Redis, you can set `message_storage` to `redis` (make sure to configure `REDIS_URL` in the `.env` file). 

This way, when deploying multiple services, the conversation can remain consistent even if requests are routed to different service instances.

default value is `local`, which means the agent will use in-memory storage for messages and history.

```yaml
agent:
  ...
  message_storage: "redis"  # Use Redis for message persistence
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

You can override the default tool name and description and add parameter descriptions using the `function_tool` decorator:

```python
@function_tool(name="custom_square", 
               description="Calculate the square of a number",
               param_descriptions={"n": "The number to square"}
def calculate_square(n: int) -> int:
    """Calculate the square of a number."""
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

# Advanced parameters for conversation control
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user123",
    "session_id": "session456",
    "user_message": "Based on our previous conversation, summarize what you know about me",
    "history_count": 25,
    "max_iter": 15,
    "max_concurrent_tools": 5,
    "stream": false
  }'
```

#### API Parameters Reference

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `user_id` | string | **required** | Unique identifier for the user |
| `session_id` | string | **required** | Unique identifier for the conversation session |
| `user_message` | string | **required** | The user's message content |
| `image_source` | string | optional | Image URL, file path, or base64 string for analysis |
| `stream` | boolean | `false` | Enable streaming response via Server-Sent Events |
| `history_count` | integer | `16` | Number of previous messages to include in context |
| `max_iter` | integer | `10` | Maximum model call attempts for complex reasoning |
| `max_concurrent_tools` | integer | `10` | Maximum number of concurrent tool calls |

#### Parameter Usage Guidelines

**`history_count`** - Controls conversation context:
- **1-5**: Independent questions without context
- **10-20**: Standard multi-turn conversations  
- **25+**: Complex conversations requiring extensive history

**`max_iter`** - Controls reasoning depth:
- **5-8**: Simple Q&A without tool usage
- **10-12**: Standard tasks with light tool usage
- **15+**: Complex multi-step reasoning and tool chains

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

  message_storage: "local"

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
  
  message_storage: "local"

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
  
  message_storage: "local"

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
        type: "field_type"        # Field type (str, int, float, bool, list)
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

User-friendly Streamlit chat interface for interactive conversations with your AI agent.

```bash
# Start the chat interface with default settings
xagent-web

# With custom agent server URL
xagent-web --agent-server http://localhost:8010

# With custom host and port
xagent-web --host 0.0.0.0 --port 8501 --agent-server http://localhost:8010
```

## 💻 Command Line Interface (CLI)

xAgent provides a powerful command-line interface for quick interactions and testing. The CLI supports both single-question mode and interactive chat sessions, with **real-time streaming responses** for a smooth conversational experience.

Note: do not support sub-agents in CLI mode currently.

### Quick Start

```bash
# Interactive chat mode with streaming (default)
xagent-cli

# Use custom configuration
xagent-cli --config my_config.yaml --toolkit_path my_toolkit --user_id developer --session_id session123 --verbose

# Ask a single question (non-streaming)
xagent-cli --ask "What is the capital of France?"

```

### Interactive Chat Mode

Start a continuous conversation with the agent with **streaming enabled by default**:

```bash
$ xagent-cli
🤖 Welcome to xAgent CLI!
Agent: Agent
Model: gpt-4.1-mini
Tools: 3 loaded
Session: cli_session_abc123
Verbose mode: Disabled
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
| `xagent-cli --ask <message>` | Ask single question (non-streaming) | `xagent-cli --ask "Hello world"` |
| `xagent-cli --init` | Create default configuration file | `xagent-cli --init` |

### CLI Options

| Option | Description | Default |
|--------|-------------|---------|
| `--config` | Configuration file path | Uses default configuration |
| `--toolkit_path` | Custom toolkit directory | No additional tools |
| `--user_id` | User identifier | Auto-generated |
| `--session_id` | Session identifier | Auto-generated |
| `--verbose`, `-v` | Enable verbose logging | `False` |
| `--no-stream` | Disable streaming response | `False` (streaming enabled) |
| `--ask` | Ask single question and exit | Interactive mode |
| `--init` | Create default configuration file | - |
| `--init-config` | Config file path for --init | `config/agent.yaml` |

## 🤖 Advanced Usage: Agent Class

For more control and customization, use the Agent class directly in your Python code.

### Basic Agent Usage

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

    # Streaming response example
    response = await agent.chat(
        user_message="Tell me a story",
        user_id="user123",
        session_id="session456", 
        stream=True
    )
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

xAgent provides powerful workflow orchestration patterns for coordinating multiple agents to solve complex tasks. Choose the right pattern based on your task requirements.

### Workflow Patterns

| Pattern | Use Case | Example |
|---------|----------|---------|
| **Sequential** | Pipeline processing, step-by-step refinement | Research → Analysis → Summary |
| **Parallel** | Consensus building, multi-perspective analysis | Multiple experts solving same problem |
| **Graph** | Complex dependencies, fan-out/fan-in patterns | A→B, A→C, B&C→D |
| **Hybrid** | Multi-stage workflows combining patterns | Research (Sequential) → Expert Review (Parallel) → Final Report (Sequential) |

### Quick Start

```python
import asyncio
from xagent import Agent
from xagent.multi.workflow import Workflow

async def workflow_example():
    # Create specialized agents
    researcher = Agent(name="Researcher", system_prompt="Research specialist")
    analyst = Agent(name="Analyst", system_prompt="Data analysis expert")
    writer = Agent(name="Writer", system_prompt="Content writing specialist")
    
    # Initialize workflow orchestrator
    workflow = Workflow()
    
    # 1. Sequential: A → B → C
    result = await workflow.run_sequential(
        agents=[researcher, analyst, writer],
        task="Research AI trends and write a summary report"
    )
    print("Sequential result:", result.result)
    
    # 2. Parallel: All agents work on same task
    result = await workflow.run_parallel(
        agents=[researcher, analyst, writer],
        task="What are the key benefits of renewable energy?"
    )
    print("Parallel consensus:", result.result)
    
    # 3. Graph: Complex dependencies with DSL support
    # Traditional dictionary format
    dependencies = {
        "Analyst": ["Researcher"],      # Analyst depends on Researcher
        "Writer": ["Researcher", "Analyst"]  # Writer depends on both
    }
    
    # NEW: DSL format (more intuitive!)
    dependencies_dsl = "Researcher→Analyst, Researcher&Analyst→Writer"
    
    result = await workflow.run_graph(
        agents=[researcher, analyst, writer],
        dependencies=dependencies_dsl,  # Use DSL format
        task="Create comprehensive market analysis"
    )
    print("Graph result:", result.result)

asyncio.run(workflow_example())
```

### Advanced: Hybrid Workflows

Combine multiple patterns into sophisticated multi-stage workflows:

```python
async def hybrid_workflow_example():
    # Create workflow stages
    stages = [
        {
            "pattern": "sequential",
            "agents": [researcher, planner],
            "task": "Research and plan: {original_task}",
            "name": "research_phase"
        },
        {
            "pattern": "parallel", 
            "agents": [expert1, expert2, expert3],
            "task": "Review this research: {previous_result}",
            "name": "expert_review"
        },
        {
            "pattern": "sequential",
            "agents": [synthesizer],
            "task": "Create final report from: {previous_result}",
            "name": "final_synthesis"
        }
    ]
    
    workflow = Workflow()
    result = await workflow.run_hybrid(
        task="Analyze the future of electric vehicles",
        stages=stages
    )
    
    print("Final result:", result["final_result"])
    print("Execution time:", result["total_execution_time"])

asyncio.run(hybrid_workflow_example())
```

### Pattern Selection Guide

**Choose Sequential when:**
- Tasks require step-by-step processing
- Each step builds on the previous one
- You need progressive refinement

**Choose Parallel when:**
- You want multiple perspectives on the same problem
- Need consensus or validation
- Quality assurance through redundancy

**Choose Graph when:**
- Tasks have complex dependencies
- Need parallel execution where possible
- Fan-out/fan-in patterns required

**Choose Hybrid when:**
- Multi-stage complex workflows
- Different stages need different patterns
- Maximum flexibility and control

### 🎯 Graph Workflow DSL (Domain Specific Language)

**NEW**: xAgent now supports an intuitive DSL for defining complex workflow dependencies using arrow notation!

#### Basic DSL Syntax

```python
# Instead of writing complex dictionaries...
dependencies = {
    "analyst": ["researcher"],
    "planner": ["researcher"], 
    "synthesizer": ["analyst", "planner"]
}

# You can now use intuitive arrows!
dependencies = "researcher→analyst, researcher→planner, analyst&planner→synthesizer"
```

#### DSL Patterns

| DSL Pattern | Description | Generated Dependencies |
|-------------|-------------|----------------------|
| `A→B` | B depends on A | `{"B": ["A"]}` |
| `A→B→C` | Chain: A→B, B→C | `{"B": ["A"], "C": ["B"]}` |
| `A→B, A→C` | Parallel branches | `{"B": ["A"], "C": ["A"]}` |
| `A&B→C` | C depends on both A and B | `{"C": ["A", "B"]}` |
| `A→B, A→C, B&C→D` | Complex fan-out/fan-in | `{"B": ["A"], "C": ["A"], "D": ["B", "C"]}` |

#### Real-World DSL Examples

```python
# Research workflow
research_flow = "collect_data→analyze_data, collect_data→create_plan, analyze_data&create_plan→write_report"

# Software development workflow  
dev_flow = "requirements→design, requirements→research, design&research→implementation, implementation→testing, testing→deployment"

# Content creation workflow
content_flow = "research→outline, research→gather_sources, outline&gather_sources→draft, draft→review→final"

# Using DSL in workflows
result = await workflow.run_graph(
    agents=[researcher, analyzer, planner, writer],
    dependencies=research_flow,  # Use DSL directly!
    task="Create comprehensive market analysis"
)
```

#### DSL Validation

```python
from xagent.multi.workflow import validate_dsl_syntax, parse_dependencies_dsl

# Validate before use
is_valid, error = validate_dsl_syntax("A→B, A→C, B&C→D")
if not is_valid:
    print(f"DSL Error: {error}")

# Parse to see the result
deps = parse_dependencies_dsl("A→B, A→C, B&C→D")
print(deps)  # {'B': ['A'], 'C': ['A'], 'D': ['B', 'C']}
```

For detailed DSL documentation, see [docs/workflow_dsl.md](docs/workflow_dsl.md).

For more examples, see the `examples/` directory with complete workflow demonstrations.

## 🏗️ Architecture

**Modern Design for High Performance**

```
xAgent/
├── 🤖 xagent/                # Core async agent framework
│   ├── __init__.py           # Package initialization and exports
│   ├── __version__.py        # Version information
│   ├── core/                 # Core agent and session management
│   │   ├── __init__.py       # Core exports (Agent, Session)
│   │   ├── agent.py          # Main Agent class with chat functionality
│   │   └── session.py        # Session management with operations
│   ├── interfaces/           # User interfaces and servers
│   │   ├── __init__.py       # Interface exports
│   │   ├── base.py           # Base interface classes
│   │   ├── cli.py            # Command line interface
│   │   ├── server.py         # HTTP Agent Server (FastAPI)
│   │   ├── web.py            # Streamlit chat application
│   │   └── web_launcher.py   # Web interface launcher
│   ├── components/           # Message storage layer
│   │   ├── __init__.py       # Database exports
│   │   ├── base_messages.py  # Abstract message storage interface
│   │   ├── local_messages.py # In-memory message storage
│   │   └── redis_messages.py # Redis-based message persistence
│   ├── schemas/              # Data models and types (Pydantic)
│   │   ├── __init__.py       # Schema exports
│   │   └── message.py        # Message and ToolCall models
│   ├── tools/                # Tool ecosystem
│   │   ├── __init__.py       # Tool registry (web_search, draw_image)
│   │   ├── openai_tool.py    # OpenAI tool integrations
│   │   └── mcp_demo/         # MCP demo server and client
│   ├── utils/                # Utility functions and helpers
│   │   ├── __init__.py       # Utility exports
│   │   ├── tool_decorator.py # Function tool decorator
│   │   ├── mcp_convertor.py  # MCP protocol conversion
│   │   └── image_upload.py   # Image processing utilities
│   └── multi/                # Multi-agent support
│       ├── __init__.py       # Multi-agent exports
│       ├── swarm.py          # Agent swarm coordination
│       └── workflow.py       # Workflow management
├── 🛠️ toolkit/               # Custom tool ecosystem
│   ├── __init__.py           # Toolkit registry
│   ├── tools.py              # Custom tools (char_count, etc.)
│   └── mcp_server.py         # Main MCP server implementation
├── ⚙️ config/                # Configuration files
│   ├── agent.yaml            # Default agent configuration
│   ├── structure_examples/   # Structured output examples
│   └── sub_agents_example/   # Multi-agent system examples
├── 📝 examples/              # Usage examples and demos
├── 🧪 tests/                 # Comprehensive test suite
├── 📁 logs/                  # Application log files
├── 📦 dist/                  # Distribution packages
└── 📄 pyproject.toml         # Project configuration
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
    sub_agents: Optional[List[Union[tuple[str, str, str], 'Agent']]] = None,
    output_type: Optional[type[BaseModel]] = None,
    message_storage: Optional[MessageStorageBase] = None
)
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
- `output_type`: Pydantic model for structured output
- `message_storage`: MessageStorageBase instance for conversation persistence

#### 🌐 HTTPAgentServer

HTTP server for agent interactions with REST API endpoints.

```python
HTTPAgentServer(
    config_path: Optional[str] = None,
    toolkit_path: Optional[str] = None
)
```

**API Endpoints:**
- `GET /health`: Health check endpoint
- `POST /chat`: Main chat interaction endpoint
- `POST /clear_session`: Clear conversation session

**Chat Endpoint (`POST /chat`) Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `user_id` | string | ✅ | - | Unique user identifier |
| `session_id` | string | ✅ | - | Conversation session identifier |
| `user_message` | string | ✅ | - | User's message content |
| `image_source` | string | ❌ | `null` | Image URL, file path, or base64 string |
| `stream` | boolean | ❌ | `false` | Enable Server-Sent Events streaming |
| `history_count` | integer | ❌ | `16` | Number of previous messages to include |
| `max_iter` | integer | ❌ | `10` | Maximum model call attempts |

**Example Usage:**
```bash
# Start server
xagent-server --config config.yaml --toolkit_path ./tools

# Basic chat
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "user123", "session_id": "session456", "user_message": "Hello!"}'

# Advanced chat with custom parameters
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user123", 
    "session_id": "session456",
    "user_message": "Analyze the conversation context",
    "history_count": 25,
    "max_iter": 15
  }'
```

#### Message Storage

xAgent provides flexible message storage options for conversation persistence.

**Available Storage Backends:**
- `MessageStorageLocal`: In-memory storage (default)
- `MessageStorageRedis`: Redis-based persistence

**Usage in Agent:**
```python
from xagent.components import MessageStorageRedis

# Create agent with Redis storage
agent = Agent(
    name="persistent_agent",
    message_storage=MessageStorageRedis()
)

# Chat calls automatically handle storage
response = await agent.chat(
    user_message="Hello",
    user_id="user123",
    session_id="session456"
)
```

**Key Interface Methods (MessageStorageBase):**
- `async add_messages(user_id, session_id, messages) -> None`: Store messages
- `async get_messages(user_id, session_id, count) -> List[Message]`: Retrieve history
- `async clear_session(user_id, session_id) -> None`: Clear conversation history


### Important Considerations

| Aspect | Details |
|--------|---------|
| **Tool functions** | Can be sync or async (automatic conversion) |
| **Agent interactions** | Always use `await` with proper user_id and session_id |
| **Context** | Run in context with `asyncio.run()` |
| **Message Storage** | Configure via Agent constructor for persistence |
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
