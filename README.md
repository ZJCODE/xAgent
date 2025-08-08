# xAgent - Multi-Modal Conversational AI System

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)](https://fastapi.tiangolo.com/)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.28+-red.svg)](https://streamlit.io/)
[![Redis](https://img.shields.io/badge/Redis-7.0+-red.svg)](https://redis.io/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

xAgent is a powerful multi-modal conversational AI system that supports text, image interactions, and vocabulary learning. Built with FastAPI backend, Streamlit frontend, and Redis for persistence, it provides a complete AI assistant experience with intelligent vocabulary management.

## ✨ Features

### 🤖 **Multi-Modal AI Chat**
- **Text Conversations**: Powered by OpenAI GPT models (GPT-4o, GPT-4o-mini, GPT-4.1)
- **Image Processing**: Upload and analyze images with AI
- **Session Management**: Persistent conversation history with Redis
- **Tool Integration**: Extensible tool system with MCP (Model Context Protocol) support

### 📚 **Intelligent Vocabulary Learning**
- **Smart Word Lookup**: AI-powered word definitions with difficulty levels
- **Personalized Vocabulary**: User-specific word learning and tracking
- **Familiarity System**: Track learning progress with smart review recommendations
- **Multi-Level Learning**: Beginner, Intermediate, and Advanced word explanations

### 🔧 **Developer-Friendly Architecture**
- **Modular Design**: Clean separation of concerns with pluggable components
- **MCP Protocol**: Model Context Protocol server for tool integration
- **Comprehensive Testing**: Full test coverage with pytest
- **Observability**: Built-in logging and monitoring with Langfuse

## 🏗️ Architecture

```
xAgent/
├── api/                    # FastAPI backend services
│   ├── main.py            # API server entry point
│   ├── health.py          # Health check endpoints
│   └── schemas/           # API data models
├── frontend/              # Streamlit web interface
│   └── chat_app.py        # Main chat application
├── xagent/                # Core agent framework
│   ├── core/              # Agent and session management
│   ├── db/                # Database layer (Redis)
│   ├── schemas/           # Data models and types
│   ├── tools/             # Tool ecosystem
│   │   ├── mcp_server.py      # MCP protocol server
│   │   ├── openai_tool.py     # OpenAI tool integrations
│   │   └── vocabulary/        # Vocabulary learning system
│   └── utils/             # Utility functions
├── examples/              # Usage examples
└── tests/                 # Test suite
```

## 🚀 Quick Start

### Prerequisites

- Python 3.12+
- Redis Server
- OpenAI API Key

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
   REDIS_URL=redis://localhost:6379
   LANGFUSE_SECRET_KEY=your_langfuse_key  # Optional
   LANGFUSE_PUBLIC_KEY=your_langfuse_public_key  # Optional
   LANGFUSE_HOST=https://cloud.langfuse.com  # Optional
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

## 💡 Usage Examples

### Basic Chat
```python
from xagent.core import Agent, Session
from xagent.db import MessageDB

# Create agent with tools
agent = Agent(
    tools=[],
    system_prompt="You are a helpful AI assistant.",
    model="gpt-4o-mini"
)

# Create session
session = Session(
    user_id="user123",
    session_id="session456",
)

# Chat
response = await agent.chat("Hello, how are you?", session)
print(response)
```

### Custom Tools
```python
from xagent.utils.tool_decorator import function_tool

@function_tool()
def calculate_sum(a: int, b: int) -> int:
    """Add two numbers together."""
    return a + b

agent = Agent(tools=[calculate_sum])
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

1. Create a tool function with the `@function_tool()` decorator
2. Add to the agent's tool list
3. Test with the provided test framework

```python
from xagent.utils.tool_decorator import function_tool

@function_tool()
def my_custom_tool(input_text: str) -> str:
    """Description of what this tool does."""
    return f"Processed: {input_text}"
```

### MCP Protocol Integration

xAgent supports the Model Context Protocol for tool integration:

```python
from xagent.utils.mcp_convertor import MCPTool

# Connect to MCP server
mcp_tool = MCPTool("http://localhost:8001/mcp/")
tools = await mcp_tool.get_openai_tools()

agent = Agent(tools=tools)
```

## 📝 API Reference

### Core Classes

#### `Agent`
Main AI agent class for handling conversations and tool execution.

```python
Agent(
    tools: List[Callable] = [],
    system_prompt: str = "You are a helpful AI assistant.",
    model: str = "gpt-4o-mini",
    name: str = "Agent"
)
```

#### `Session`
Manages conversation history and persistence.

```python
Session(
    user_id: str,
    session_id: Optional[str] = None,
    message_db: Optional[MessageDB] = None
)
```

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
