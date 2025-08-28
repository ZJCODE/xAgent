# xAgent - Multi-Modal AI Agent System

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)](https://fastapi.tiangolo.com/)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.28+-red.svg)](https://streamlit.io/)
[![Redis](https://img.shields.io/badge/Redis-7.0+-red.svg)](https://redis.io/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **🚀 A powerful and easy-to-use AI Agent system with real-time streaming responses**

xAgent provides a complete AI assistant experience with text and image processing, tool execution, HTTP server, web interface, and streaming CLI. **Production-ready with full multi-user and multi-session support** - perfect for both quick prototypes and enterprise deployments.

Also includes advanced features like multi-agent workflows even with **intelligent automatic workflow generation** that analyzes your task and creates optimal multi-agent coordination patterns.

## 📋 Table of Contents

- [🚀 Quick Start](#-quick-start)
- [🔧 Installation](#-installation)
- [🌐 HTTP Server](#-http-server)
- [🌐 Web Interface](#-web-interface)
- [💻 Command Line Interface](#-command-line-interface)
- [🤖 Python API](#-python-api)
- [🧠 Memory System](#-memory-system)
- [🔄 Multi-Agent Workflows](#-multi-agent-workflows)
- [📚 Documentation](#-documentation)
- [🤝 Contributing](#-contributing)
- [📄 License](#-license)


## Roadmap

- [ ] Add Voice Support
- [ ] Workflow Add Service Support

## 🚀 Quick Start

Get up and running with xAgent in just a few commands:

```bash
# Install xAgent
pip install myxagent

# Set your OpenAI API key
export OPENAI_API_KEY=your_openai_api_key

# Start interactive CLI chat with default agent 
xagent-cli

# Or quickly ask a question
xagent-cli --ask "who are you?"
xagent-cli --ask "what's the weather in Hangzhou and Shanghai?" -v

```

That's it!

### Starting the HTTP Server

The easiest way to deploy xAgent in production is through the HTTP server. **Production-ready with full multi-user and multi-session support** - handle thousands of concurrent users with isolated conversation histories.

```bash
# Start the HTTP server with default agent configuration
xagent-server

# Server runs at http://localhost:8010
```

> if you want to customize the agent, read more at [🌐 HTTP Server](#-http-server)

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

### Docker Deployment

For production deployments, you can use Docker to run xAgent with all dependencies.

Find the Docker setup in the `deploy/docker/` directory. See the [Docker README](deploy/docker/README.md) for detailed instructions.

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

<details>
<summary><b>Initialize Project Config (Automatic)</b></summary>

Create default config and toolkit structure, run below command:

```bash 
xagent-cli --init
```

This creates:
- `config/agent.yaml` - Configuration file
- `my_toolkit/` - Custom tools directory with examples
</details>


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
    mcp_servers:
      - "http://localhost:8001/mcp/"  # Example MCP server

server:
  host: "0.0.0.0"
  port: 8010
```

For advanced configurations including structured outputs and multi-agent systems, see [Configuration Reference](docs/configuration_reference.md).

### Custom Tools

Create `my_toolkit/` directory with `__init__.py` and `tools.py`:

```python
# my_toolkit/__init__.py
from .tools import *

TOOLKIT_REGISTRY = {
    "calculate_square": calculate_square,
    "fetch_weather": fetch_weather
}

# my_toolkit/tools.py
import asyncio
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
# Use the generated config and toolkit
xagent-server --config config/agent.yaml --toolkit_path my_toolkit
```

### API Usage

```bash
# Multi-user chat examples
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user123",
    "session_id": "session456", 
    "user_message": "Calculate the square of 15"
  }'

# Different user, different session
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "alice",
    "session_id": "work_session", 
    "user_message": "What is the weather in Tokyo?"
  }'

# Same user, different session for separate conversation
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "alice",
    "session_id": "personal_session", 
    "user_message": "Help me plan a vacation"
  }'

# Image analysis with URL
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "alice",
    "session_id": "image_session", 
    "user_message": "What do you see in this image?",
    "image_source": "https://example.com/image.jpg"
  }'

# Multiple images can be sent as a list
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "alice",
    "session_id": "image_session",
    "user_message": "What do you see in these images?",
    "image_source": [
      "https://example.com/image1.jpg",
      "https://example.com/image2.jpg"
    ]
  }'

# Streaming response for any user/session
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "bob",
    "session_id": "session789",
    "user_message": "Tell me a story",
    "stream": true
  }'
```


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
╭──────────────────────────────────────────────────────────╮
│                  🤖 Welcome to xAgent CLI!               │
╰──────────────────────────────────────────────────────────╯

📁 Config: Default configuration
🤖 Agent: Agent
🧠 Model: gpt-4o-mini
🛠️  Tools: 1 loaded
🔗 Session: cli_session_d02daf21
⚙️  Status: 🔇 Verbose: Off | 🌊 Stream: On

────────────────────────────────────────────────────────────
🚀 Quick Start:
  • Type your message to chat with the agent
  • Use 'help' to see all available commands
  • Use 'exit', 'quit', or 'bye' to end session
  • Use 'clear' to reset conversation history
  • Use 'stream on/off' to toggle response streaming
────────────────────────────────────────────────────────────

👤 You: Hello, how are you?
🤖 Agent: Hello! I'm doing well, thank you for asking...

👤 You: help

╭─ 📋 Commands ─────────────────────────────────────────────╮
│ exit, quit, bye    Exit the chat session                  │
│ clear              Clear conversation history             │
│ stream on/off      Toggle streaming response mode         │
│ help               Show this help message                 │
╰───────────────────────────────────────────────────────────╯

╭─ 🔧 Built-in Tools ───────────────────────────────────────╮
│  1. web_search                                            │
╰───────────────────────────────────────────────────────────╯

👤 You: exit

╭───────────────────────────────────────╮
│  👋 Thank you for using xAgent CLI!   │
│         See you next time! 🚀         │
╰───────────────────────────────────────╯
```

### CLI Options

| Option | Description | Example |
|--------|-------------|---------|
| `--config` | Configuration file path | `--config my_config.yaml` |
| `--toolkit_path` | Custom toolkit directory | `--toolkit_path my_toolkit` |
| `--user_id` | User ID for session | `--user_id user123` |
| `--session_id` | Session ID for chat | `--session_id session456`
| `--ask` | Ask single question and exit | `--ask "Hello world"` |
| `--init` | Create default config and toolkit | `--init` |
| `--verbose`, `-v` | Enable verbose logging | `--verbose` |

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

    # Text chat interaction
    response = await agent.chat(
        user_message="Hello, how are you?",
        user_id="user123", 
        session_id="session456"
    )
    print(response)
    
    # Image analysis with URL
    response = await agent.chat(
        user_message="What do you see in this image?",
        user_id="user123",
        session_id="session456",
        image_source="https://example.com/image.jpg"
    )
    print(response)

    # Multiple images can be sent as a list
    response = await agent.chat(
        user_message="What do you see in these images?",
        user_id="user123",
        session_id="session456",
        image_source=[
            "https://example.com/image1.jpg",
            "https://example.com/image2.jpg"
        ]
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

For detailed guidance, see the [Message Storage Inheritance](docs/message_storage_inheritance.md) documentation.

### Long-term Memory

```python
import asyncio
from xagent.core import Agent
from xagent.components.memory import MemoryStorageLocal

async def memory_example():
    # Create agent with long-term memory
    memory_storage = MemoryStorageLocal(
        collection_name="user_memories", 
        memory_threshold=5  # Store memories after 5 messages
    )
    
    agent = Agent(
        name="memory_assistant",
        system_prompt="You are a helpful assistant with excellent memory.",
        memory_storage=memory_storage
    )
    
    # First conversation - store user information
    response1 = await agent.chat(
        user_message="Hi, I'm Alice. I work as a product manager at Apple and I love Italian food.",
        user_id="alice_123",
        session_id="intro",
        enable_memory=True
    )
    print("First response:", response1)
    
    # Later conversation - agent remembers Alice's details
    response2 = await agent.chat(
        user_message="Can you recommend a good restaurant for lunch?",
        user_id="alice_123", 
        session_id="restaurant_request",
        enable_memory=True
    )
    print("Memory-enhanced response:", response2)
    # Agent will remember Alice's preference for Italian food

asyncio.run(memory_example())
```

### HTTP Server with Agent Instance

Launch an HTTP server by directly passing a pre-configured Agent instance:

```python
import asyncio
from xagent.core import Agent
from xagent.interfaces.server import AgentHTTPServer
from xagent.tools import web_search

# Create a custom agent with specific tools and configuration
agent = Agent(
    name="MyCustomAgent",
    system_prompt="You are a helpful research assistant specialized in web search and analysis.",
    model="gpt-4o",
    tools=[web_search]
)

# Start HTTP server with the agent
server = AgentHTTPServer(agent=agent)
server.run(host="0.0.0.0", port=8010)

# Server is now running at http://localhost:8010
# You can interact via API:
# curl -X POST "http://localhost:8010/chat" \
#   -H "Content-Type: application/json" \
#   -d '{"user_id": "user123", "session_id": "session456", "user_message": "Hello!"}'
```

## 🧠 Memory System

xAgent includes a powerful memory system that enables agents to store, recall, and utilize long-term memories across conversations. The memory system automatically extracts and stores important information from conversations, allowing agents to maintain context and personalize responses over time.

### Quick Start with Memory

```python
import asyncio
from xagent.core import Agent

async def main():
    # Create agent with memory enabled
    agent = Agent(
        name="memory_assistant",
        system_prompt="You are a helpful assistant with long-term memory."
    )
    
    # Chat with memory enabled
    response = await agent.chat(
        user_message="Hi, I'm John. I work as a software engineer at Google and live in San Francisco.",
        user_id="john_123",
        session_id="session_1",
        enable_memory=True  # Enable memory for this conversation
    )
    print(response)
    
    # In a later conversation, the agent will remember John's details
    response = await agent.chat(
        user_message="What do you know about me?",
        user_id="john_123", 
        session_id="session_2",
        enable_memory=True
    )
    print(response)  # Agent will recall John's job and location

asyncio.run(main())
```

### Memory Storage Options

xAgent supports multiple memory storage backends:

- **Local Memory (ChromaDB)**: Default option, stores data locally
- **Upstash Vector**: Cloud-based vector storage for production

```python
from xagent.components.memory import MemoryStorageLocal, MemoryStorageUpstash

# Local ChromaDB storage
local_memory = MemoryStorageLocal(
    collection_name="my_agent_memory",
    memory_threshold=10,  # Store after 10 messages or automatically trigger
    keep_recent=2  # Keep 2 recent messages after storage
)

# Upstash Vector storage (requires UPSTASH_VECTOR_* env vars)
upstash_memory = MemoryStorageUpstash(
    memory_threshold=10,
    keep_recent=2
)

# Use with agent
agent = Agent(
    name="assistant",
    memory_storage=local_memory  # or upstash_memory
)
```

### Environment Variables for Memory

For Upstash Vector storage, set these environment variables:

```bash
# Upstash Vector Database
UPSTASH_VECTOR_REST_URL=your_upstash_vector_url
UPSTASH_VECTOR_REST_TOKEN=your_upstash_vector_token

# Redis for temporary message storage (with Upstash)
REDIS_URL=your_redis_url
```

**Key Features:**
- ✅ Automatic memory extraction from conversations
- ✅ Semantic search and retrieval 
- ✅ Multiple storage backends (Local ChromaDB, Upstash Vector)
- ✅ Configurable memory thresholds
- ✅ LLM-powered memory processing
- ✅ User-isolated memory storage

For detailed memory system documentation, see [Memory Documentation](docs/memory.md).


## 🔄 Multi-Agent Workflows

xAgent features **intelligent automatic workflow generation** that analyzes your task and creates optimal multi-agent coordination patterns.

### Workflow Patterns

| Pattern | Use Case | Setup Required | AI Optimization |
|---------|----------|----------------|-----------------|
| **Auto** | Any complex task | None - just describe the task | Full AI analysis and optimization |
| **Sequential** | Pipeline processing | Manual agent design | None |
| **Parallel** | Multiple perspectives | Manual agent design | None |
| **Graph** | Complex dependencies | Manual agent + dependency design | None |
| **Hybrid** | Multi-stage workflows | Manual stage configuration | None |

### Quick Start Examples


### 🌟 Auto Workflow (AI-Powered)

Zero configuration required - AI automatically creates optimal agent teams:

```python
import asyncio
from xagent.multi.workflow import Workflow

async def auto_workflow_example():
    workflow = Workflow()
    
    # AI creates optimal agents and dependencies automatically
    result = await workflow.run_auto(
        task="Develop a comprehensive go-to-market strategy for a new SaaS product targeting healthcare providers"
    )
    
    print(f"✅ AI created {result.metadata['agent_count']} specialized agents:")
    for agent in result.metadata['generated_agents']:
        print(f"  • {agent['name']}: {agent['system_prompt'][:80]}...")
    print(f"🔗 Generated dependencies: {result.metadata['generated_dependencies']}")
    print(f"🧠 AI reasoning: {result.metadata['agent_selection_reasoning']}")
    print(f"📊 Result: {result.result}")

asyncio.run(auto_workflow_example())
```


<details>
<summary><b>🤖 Agent Definitions (Click to expand)</b></summary>

```python
from xagent import Agent

# Market Research Specialist
market_researcher = Agent(
    name="MarketResearcher",
    system_prompt="""You are a senior market research analyst with 10+ years of experience. 
    Your expertise includes:
    - Industry trend analysis and forecasting
    - Competitive landscape assessment
    - Market size estimation and growth projections
    - Consumer behavior analysis
    - Technology adoption patterns
    
    Always provide data-driven insights with specific metrics, sources, and actionable recommendations.""",
    model="gpt-4o"
)

# Data Science Specialist
data_scientist = Agent(
    name="DataScientist", 
    system_prompt="""You are a senior data scientist specializing in business intelligence and predictive analytics.
    Your core competencies:
    - Statistical analysis and hypothesis testing
    - Predictive modeling and machine learning
    - Data visualization and storytelling
    - Risk assessment and scenario planning
    - Performance metrics and KPI development
    
    Transform raw research into quantitative insights, identify patterns, and build predictive models.""",
    model="gpt-4o"
)

# Business Writing Specialist
business_writer = Agent(
    name="BusinessWriter",
    system_prompt="""You are an executive business writer and strategic communications expert.
    Your specializations:
    - Executive summary creation
    - Strategic recommendation development
    - Stakeholder communication
    - Risk and opportunity assessment
    - Implementation roadmap design
    
    Create compelling, actionable business reports that drive decision-making at the C-level.""",
    model="gpt-4o"
)

# Financial Analysis Specialist
financial_analyst = Agent(
    name="FinancialAnalyst",
    system_prompt="""You are a CFA-certified financial analyst with expertise in valuation and investment analysis.
    Your focus areas:
    - Financial modeling and valuation
    - Investment risk assessment
    - ROI and NPV calculations
    - Capital allocation strategies
    - Market opportunity sizing
    
    Provide detailed financial analysis with concrete numbers, projections, and investment recommendations.""",
    model="gpt-4o"
)

# Strategy Consulting Specialist
strategy_consultant = Agent(
    name="StrategyConsultant",
    system_prompt="""You are a senior strategy consultant from a top-tier consulting firm.
    Your expertise includes:
    - Strategic planning and execution
    - Business model innovation
    - Competitive strategy development
    - Organizational transformation
    - Change management
    
    Synthesize complex information into clear strategic recommendations with implementation timelines.""",
    model="gpt-4o"
)

# Quality Assurance Specialist
quality_reviewer = Agent(
    name="QualityReviewer",
    system_prompt="""You are a senior partner-level consultant specializing in quality assurance and risk management.
    Your responsibilities:
    - Strategic recommendation validation
    - Risk identification and mitigation
    - Stakeholder impact assessment
    - Implementation feasibility review
    - Compliance and regulatory considerations
    
    Ensure all recommendations are practical, well-researched, and aligned with business objectives.""",
    model="gpt-4o"
)
```
</details>

### 📋 Sequential Workflow

Pipeline processing where each agent builds on the previous output:

```python
import asyncio
from xagent.multi.workflow import Workflow

async def sequential_workflow_example():
    workflow = Workflow()
    
    # Research → Analysis → Report Pipeline
    result = await workflow.run_sequential(
        agents=[market_researcher, data_scientist, business_writer],
        task="Analyze the electric vehicle charging infrastructure market opportunity in North America for 2024-2027"
    )
    
    print("Sequential Pipeline Result:", result.result)

asyncio.run(sequential_workflow_example())
```

### 🔄 Parallel Workflow

Multiple expert perspectives on the same challenge:

```python
import asyncio
from xagent.multi.workflow import Workflow

async def parallel_workflow_example():
    workflow = Workflow()
    
    # Multiple experts analyze the same problem simultaneously
    # Note: Can set output_type for structured consensus validation
    result = await workflow.run_parallel(
        agents=[financial_analyst, strategy_consultant, data_scientist],
        task="Evaluate the investment potential and strategic implications of generative AI adoption in enterprise software companies"
    )
    
    print("Expert Panel Analysis:", result.result)

asyncio.run(parallel_workflow_example())
```

### 🕸️ Graph Workflow

Complex dependency networks with conditional execution:

```python
import asyncio
from xagent.multi.workflow import Workflow

async def graph_workflow_example():
    workflow = Workflow()
    
    # Complex dependency analysis
    dependencies = "MarketResearcher->DataScientist, MarketResearcher->FinancialAnalyst, DataScientist&FinancialAnalyst->StrategyConsultant, StrategyConsultant->BusinessWriter"
    
    result = await workflow.run_graph(
        agents=[market_researcher, data_scientist, financial_analyst, strategy_consultant, business_writer],
        dependencies=dependencies,
        task="Develop a comprehensive go-to-market strategy for a B2B SaaS startup entering the healthcare analytics space"
    )
    
    print("Strategic Analysis Result:", result.result)

asyncio.run(graph_workflow_example())
```

### 🔀 Hybrid Workflow

Multi-stage workflows combining different patterns:

```python
import asyncio
from xagent.multi.workflow import Workflow

async def hybrid_workflow_example():
    workflow = Workflow()
    
    # Multi-stage comprehensive business analysis
    # Note: Tasks can include placeholders like {previous_result} and {original_task}
    stages = [
        {
            "pattern": "sequential",
            "agents": [market_researcher, financial_analyst],
            "task": "Conduct market and financial analysis for: {original_task}",
            "name": "market_financial_analysis"
        },
        {
            "pattern": "parallel", 
            "agents": [data_scientist, strategy_consultant],
            "task": "Analyze strategic implications and develop data-driven insights based on: {previous_result}",
            "name": "strategic_data_analysis"
        },
        {
            "pattern": "graph",
            "agents": [business_writer, quality_reviewer, strategy_consultant],
            "dependencies": "BusinessWriter->QualityReviewer, StrategyConsultant->QualityReviewer",
            "task": "Create final strategic report with quality validation from: {previous_result}",
            "name": "report_synthesis_validation"
        }
    ]
    
    result = await workflow.run_hybrid(
        task="Develop a digital transformation strategy for a traditional manufacturing company looking to implement IoT and predictive maintenance solutions",
        stages=stages
    )
    
    print("Comprehensive Strategy Report:", result["final_result"])

asyncio.run(hybrid_workflow_example())
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
- [Memory System](docs/memory.md) - Long-term memory capabilities and storage backends
- [Multi-Agent Workflows](docs/workflows.md) - Workflow patterns and orchestration examples
- [Workflow DSL](docs/workflow_dsl.md) - Domain-specific language for defining agent dependencies

### Examples
- [examples/demo](examples/demo) - Complete usage examples and demos
- [examples/config/](examples/config/) - Configuration file templates
- [examples/toolkit/](examples/toolkit/) - Custom tool development examples

### Deployment

- [deploy/docker/](deploy/docker/) - Production deployment with Docker and Docker Compose

### Architecture Overview

xAgent is built with a modular architecture:

- **Core (`xagent/core/`)** - Agent and session management
- **Interfaces (`xagent/interfaces/`)** - CLI, HTTP server, and web interface
- **Components (`xagent/components/`)** - Message storage and persistence
- **Tools (`xagent/tools/`)** - Built-in and custom tool ecosystem
- **Multi (`xagent/multi/`)** - Multi-agent coordination and workflows

### Key Features

- **🌟 Auto Workflow** - Revolutionary AI-powered automatic workflow generation with zero configuration
- **🏢 Production Ready** - Multi-user and multi-session support with enterprise-grade reliability
- **👥 Multi-User Support** - Concurrent user handling with isolated sessions and conversation histories
- **🔄 Multi-Session Management** - Each user can maintain multiple independent conversation sessions
- **🚀 Easy to Use** - Simple API for quick prototyping
- **⚡ High Performance** - Async/await throughout, concurrent tool execution
- **🧠 Intelligent Agent Design** - AI creates optimal specialist teams (2-6 agents) based on task complexity
- **🔗 Smart Dependencies** - Automatic dependency optimization for maximum parallel execution
- **🔧 Extensible** - Custom tools, MCP integration, plugin system
- **🌐 Multiple Interfaces** - CLI, HTTP API, web interface
- **💾 Persistent** - Redis-backed conversation storage
- **🤖 Multi-Agent** - Hierarchical agent systems and workflows
- **📊 Observable** - Built-in logging and monitoring

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
- `image_source`: Optional image(s) for analysis (URL, path, or base64)
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
