# xAgent - 多模态 AI 代理系统

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)](https://fastapi.tiangolo.com/)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.28+-red.svg)](https://streamlit.io/)
[![Redis](https://img.shields.io/badge/Redis-7.0+-red.svg)](https://redis.io/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **🚀 一个功能强大的多模态 AI 代理系统，支持实时流式响应**

xAgent 提供端到端的 AI 助手体验：支持文本与图像处理、高性能并发工具执行、易用的 HTTP 服务器、Web 界面，以及实时流式 CLI。基于 FastAPI、Streamlit 和 Redis 构建，面向生产级扩展。

## 📋 目录

- [🚀 快速开始](#-快速开始)
- [🚀 安装与配置](#-安装与配置)
- [🌐 HTTP 代理服务器](#-http-代理服务器)
- [🌐 Web 界面](#-web-界面)
- [💻 命令行界面 (CLI)](#-命令行界面-cli)
- [🤖 高级用法：Agent 类](#-高级用法agent-类)
- [🏗️ 架构](#%EF%B8%8F-架构)
- [🤖 API 参考](#-api-参考)
- [📊 监控与可观测性](#-监控与可观测性)
- [🤝 贡献](#-贡献)
- [📄 许可证](#-许可证)


## 🚀 快速开始

快速开始使用 xAgent，安装包并设置您的 OpenAI API 密钥。然后，您可以运行 `CLI` 或 `HTTP 服务器` 与您的 AI 代理交互。

```bash
# 安装 xAgent
pip install myxagent

# 设置您的 OpenAI API 密钥
export OPENAI_API_KEY=your_openai_api_key

# 使用默认配置启动 CLI
xagent-cli

# 或使用默认配置启动 HTTP 服务器
xagent-server

# 启动 Streamlit Web 界面（可选）
xagent-web
```

如果启动 HTTP 服务器，您可以使用以下命令与代理交互：

```bash
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user123",
    "session_id": "session456",
    "user_message": "你是谁？",
    "stream": false
  }'
```

有关 CLI 和 HTTP 服务器使用的更多信息，请参阅 [CLI](#-命令行界面-cli) 和 [HTTP 代理服务器](#-http-代理服务器) 部分。


## 🚀 安装与配置

### 先决条件

| 需求 | 版本 | 目的 |
|-------------|---------|---------|
| **Python** | 3.12+ | 核心运行时 |
| **OpenAI API 密钥** | - | AI 模型访问 |

### 通过 pip 安装

```bash
pip install myxagent

# 使用官方 PyPI
pip install myxagent -i https://pypi.org/simple

# 或在中国使用阿里云镜像加速下载
pip install myxagent -i https://mirrors.aliyun.com/pypi/simple
```


### 环境配置

在项目目录中创建 `.env` 文件，并添加以下变量：

```bash
# 必填
OPENAI_API_KEY=your_openai_api_key

# 可选 - Redis 持久化
REDIS_URL=your_redis_url_with_password

# 可选 - 可观测性
LANGFUSE_SECRET_KEY=your_langfuse_key
LANGFUSE_PUBLIC_KEY=your_langfuse_public_key
LANGFUSE_HOST=https://cloud.langfuse.com

# 可选 - 图像上传到 S3
AWS_ACCESS_KEY_ID=your_aws_access_key_id
AWS_SECRET_ACCESS_KEY=your_aws_secret_access_key
AWS_REGION=us-east-1
BUCKET_NAME=your_bucket_name
```


## 🌐 HTTP 代理服务器

使用 xAgent 最简单的方法是通过 HTTP 服务器。只需创建配置文件并开始服务！

### 1. 创建代理配置

创建 `agent_config.yaml`:

```yaml
agent:
  name: "MyAgent"
  system_prompt: |
    You are a helpful assistant. Your task is to assist users with their queries and tasks.
  model: "gpt-4.1-mini"

  capabilities:
    tools:
      - "web_search"  # 内置网络搜索
      - "draw_image"  # 内置图像生成（需要在 .env 中设置 AWS 凭证）
      - "calculate_square"  # 来自 my_toolkit 的自定义工具
      
server:
  host: "0.0.0.0"
  port: 8010
```

如果您想使用 MCP（模型上下文协议）进行动态工具加载，您还可以在代理配置中添加 `mcp_servers`

在 `toolkit/mcp_server.py` 中可以找到启动 MCP 服务器的示例：

```yaml
agent:
  ...
  capabilities:
    mcp_servers:
      - "http://localhost:8001/mcp/"  # MCP 服务器 URL
  ...
```

如果您使用 Redis，可以将 `use_local_session` 设置为 `false` （确保在 `.env` 文件中配置 `REDIS_URL`）。这样，在部署多个服务时，即使请求路由到不同的服务实例，谈话也可以保持一致。

```yaml
agent:
  ...
  use_local_session: false
  ...
```

### 2. 创建自定义工具（可选）

创建 `my_toolkit/` 目录，包含 `__init__.py` 和您的工具函数脚本，例如 `your_tools.py`：

```python
# my_toolkit/__init__.py
from .your_tools import calculate_square, greet_user

# 代理将自动发现这些工具，您可以选择在代理配置中加载哪些工具
TOOLKIT_REGISTRY = {
    "calculate_square": calculate_square,
    "fetch_weather": fetch_weather
}

```

在 `your_tools.py` 中实现您的工具：

```python
# my_toolkit/your_tools.py
from xagent.utils.tool_decorator import function_tool

@function_tool()
def calculate_square(n: int) -> int:
    """计算一个数字的平方。"""
    return n * n

@function_tool()
async def fetch_weather(city: str) -> str:
    """获取某个城市的天气数据（虚拟实现）。"""
    return f"{city} 的天气晴朗，最高气温 25°C。"

```

您可以使用 `function_tool` 装饰器覆盖默认的工具名称和描述：

```python
@function_tool(name="custom_square", description="计算一个数字的平方")
def calculate_square(n: int) -> int:
    return n * n
```

### 3. 启动服务器

```bash
# 使用默认配置启动 HTTP 代理服务器
xagent-server

# 使用自定义配置和工具包
xagent-server --config agent_config.yaml --toolkit_path my_toolkit

# 服务器将可用在 http://localhost:8010
```

### 4. 使用 API

```bash
# 简单的聊天请求
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user123",
    "session_id": "session456",
    "user_message": "计算 15 的平方并问候我，称呼我为 Alice"
  }'

# 流式响应
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user123",
    "session_id": "session456",
    "user_message": "你好，你好吗？",
    "stream": true
  }'
```

### 5. 高级配置 （分层多代理系统）

xAgent 支持复杂的多代理架构和高级配置选项，以满足复杂用例的需求。（代理作为工具模式）

#### 多代理系统与子代理

创建一个分层代理系统，其中协调代理将任务委派给专门的子代理：

主代理配置 (`coordinator_agent.yaml`):
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
      - "char_count" # 自定义字符计数工具
    mcp_servers:
      - "http://localhost:8001/mcp/"
  
  sub_agents:
    - name: "research_agent"
      description: "专注于信息收集和分析的研究代理"
      server_url: "http://localhost:8011"
    - name: "write_agent"
      description: "处理写作任务，包括内容创作和编辑的专家代理"
      server_url: "http://localhost:8012"

  use_local_session: true

server:
  host: "0.0.0.0"
  port: 8010
```

研究专家 (`research_agent.yaml`):
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
      - "web_search" # 内置网络搜索工具
    mcp_servers:
      - "http://localhost:8002/mcp/"
  
  use_local_session: true

server:
  host: "0.0.0.0"
  port: 8011
```

写作专家 (`writing_agent.yaml`):
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

#### 启动多代理系统

```bash
# 首先启动子代理
xagent-server --config research_agent.yaml > logs/research.log 2>&1 &
xagent-server --config writing_agent.yaml > logs/writing.log 2>&1 &

# 启动协调代理
xagent-server --config coordinator_agent.yaml --toolkit_path my_toolkit > logs/coordinator.log 2>&1 &

# 验证所有代理均在运行
curl http://localhost:8010/health
curl http://localhost:8011/health
curl http://localhost:8012/health

# 现在您可以通过其 API 与协调代理聊天
curl -X POST "http://localhost:8010/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user123",
    "session_id": "session456",
    "user_message": "研究可再生能源的好处并写一份简要总结。"
  }'
```

您可以创建任意深度的子代理，形成代理的分层树状结构。只需确保没有循环引用，并按自下而上的顺序启动代理。

### 6. 高级配置 （使用 Pydantic 模型的结构化输出）

xagent 现在支持直接在 YAML 配置文件中定义结构化输出架构。此功能允许您使用 Pydantic 模型指定预期的输出格式，确保类型安全和易于解析代理的响应。

#### 结构化输出配置

在您的 YAML 配置文件中，您可以像这样定义 `output_schema`：

```yaml
agent:
  name: "YourAgent"
  system_prompt: "Your system prompt here"
  model: "gpt-4o-mini"
  
  output_schema:
    class_name: "YourModelName"  # Pydantic 模型类名
    fields:
      field_name:
        type: "field_type"        # 字段类型 (str, int, float, bool, list, dict)
        description: "description"    # 字段描述
      list_field:
        type: "list"
        items: "str"              # 列表项类型（仅对列表字段必需）
        description: "A list of strings"
```

#### 支持的字段类型

- `str` - 字符串类型
- `int` - 整数类型
- `float` - 浮点数类型
- `bool` - 布尔类型
- `list` - 列表类型
- `dict` - 字典类型


重要说明：
- 使用 `list` 类型时，必须通过 `items` 字段指定元素类型。
- 这是为了遵循 OpenAI JSON Schema 验证要求。
- `items` 支持任何基本类型：`str`、`int`、`float`、`bool` 等。

内容生成模型的示例（带有列表字段）：

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
        items: "str"  # 图像 URL 列表，字符串类型
        description: "与内容相关的图像 URL 列表"
      tags:
        type: "list"
        items: "str"
        description: "相关标签的列表"
```

上述配置将自动生成以下 Pydantic 模型：

```python
from typing import List
from pydantic import BaseModel, Field

class ContentReport(BaseModel):
    title: str = Field(description="The title of the content report")
    content: str = Field(description="The main content of the report")
    images: List[str] = Field(description="List of image URLs related to the content")
    tags: List[str] = Field(description="List of relevant tags")
```

以这种方式启动的代理将在对话期间自动创建 Pydantic 模型，并返回结构化输出。

## 🌐 Web 界面

xAgent 提供了用户友好的 Streamlit Web 界面，用于与您的 AI 代理进行交互式对话。

### 启动 Web 界面

```bash
# 使用默认设置启动 Web 界面
xagent-web

# 使用自定义代理服务器 URL
xagent-web --agent-server http://localhost:8010

# 使用自定义主机和端口
xagent-web --host 0.0.0.0 --port 8501 --agent-server http://localhost:8010
```

### Web 界面选项

| 选项 | 描述 | 默认 |
|--------|-------------|---------|
| `--agent-server` | xAgent 服务器的 URL | `http://localhost:8010` |
| `--host` | Streamlit 服务器的主机地址 | `0.0.0.0` |
| `--port` | Streamlit 服务器的端口 | `8501` |

### 完整的 Web 设置示例

```bash
# 终端 1：启动代理服务器
xagent-server --config agent_config.yaml --toolkit_path my_toolkit

# 终端 2：启动 Web 界面
xagent-web --agent-server http://localhost:8010

# 访问 Web 界面：http://localhost:8501
```

## 💻 命令行界面 (CLI)

xAgent 提供了强大的命令行界面，用于快速交互和测试。CLI 支持单问答模式和交互式聊天会话，具有 **实时流式响应**，提供流畅的对话体验。

注意：当前不支持 CLI 模式中的子代理。

### 快速开始

```bash
# 交互式聊天模式，支持流式（默认）
xagent-cli

# 使用自定义配置
xagent-cli chat --config my_config.yaml --toolkit_path my_toolkit --user_id developer --session_id session123 --verbose

# 提出单个问题（非流式）
xagent-cli ask "法国的首都是什么？"

```

### 交互式聊天模式

与代理进行持续对话，**默认启用流式**：

```bash
$ xagent-cli chat
🤖 欢迎使用 xAgent CLI！
代理： Agent
模型： gpt-4.1-mini
工具： 3 个已加载
会话： cli_session_abc123
流式： 启用
输入 'exit'、'quit' 或 'bye' 结束会话。
输入 'clear' 清除会话历史记录。
输入 'stream on/off' 切换流式模式。
输入 'help' 获取可用命令。

👤 你：你好，你好吗？
🤖 代理：你好！我很好，谢谢你的关心...
[响应实时流式传输]

👤 你：帮助
📋 可用命令：
  exit, quit, bye  - 退出聊天会话
  clear           - 清除会话历史
  stream on/off   - 切换流式模式
  help            - 显示此帮助信息

👤 你：退出
👋 再见！
```

### CLI 命令参考

| 命令 | 描述 | 示例 |
|---------|-------------|---------|
| `xagent-cli` | 启动交互式聊天（默认支持流式） | `xagent-cli` |
| `xagent-cli chat` | 明确启动交互式聊天 | `xagent-cli chat --config my_config.yaml` |
| `xagent-cli ask <message>` | 提出单个问题（非流式） | `xagent-cli ask "Hello world"` |

### CLI 选项

| 选项 | 描述 | 默认 |
|--------|-------------|---------|
| `--config` | 配置文件路径 | `config/agent.yaml` |
| `--toolkit_path` | 自定义工具包目录 | `toolkit` |
| `--user_id` | 用户标识符 | 自动生成 |
| `--session_id` | 会话标识符 | 自动生成 |
| `--verbose`, `-v` | 启用详细日志 | `False` |

## 🤖 高级用法：Agent 类

要获得更多控制和自定义，请直接在 Python 代码中使用 Agent 类。

### 基本代理用法

```python
import asyncio
from xagent.core import Agent, Session

async def main():
    # 创建代理
    agent = Agent(
        name="my_assistant",
        system_prompt="You are a helpful AI assistant.",
        model="gpt-4.1-mini"
    )

    # 为会话管理创建会话
    session = Session(session_id="session456")

    # 聊天交互
    response = await agent.chat("Hello, how are you?", session)
    print(response)

    # 流式响应示例
    response = await agent.chat("Tell me a story", session, stream=True)
    async for event in response:
        print(event, end="")

asyncio.run(main())
```

### 添加自定义工具

```python
import asyncio
import time
import httpx
from xagent.utils.tool_decorator import function_tool
from xagent.core import Agent, Session

# 同步工具 - 自动转换为异步
@function_tool()
def calculate_square(n: int) -> int:
    """计算一个数字的平方。"""
    time.sleep(0.1)  # 模拟 CPU 工作
    return n * n

# 异步工具 - 直接用于 I/O 操作
@function_tool()
async def fetch_weather(city: str) -> str:
    """从 API 获取天气数据。"""
    async with httpx.AsyncClient() as client:
        await asyncio.sleep(0.5)  # 模拟 API 调用
        return f"{city} 的天气：22°C，晴天"

async def main():
    # 创建带有自定义工具的代理
    agent = Agent(
        tools=[calculate_square, fetch_weather],
        model="gpt-4.1-mini"
    )
    
    session = Session(user_id="user123")
    
    # 代理自动处理所有工具
    response = await agent.chat(
        "计算 15 的平方并获取东京的天气",
        session
    )
    print(response)

asyncio.run(main())
```

### 使用结构化输出和 Pydantic

使用 Pydantic 结构化输出，您可以：
- 解析和验证代理的响应为类型安全的数据
- 轻松提取特定字段
- 确保响应符合预期格式
- 保证应用程序中的类型安全
- 可靠地链式处理多步骤任务，使用结构化数据

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
                  output_type=WeatherReport) # 您可以在这里设置默认输出类型，也可以将其留空
    
    session = Session(user_id="user123")
    
    # 请求天气的结构化输出
    weather_data = await agent.chat(
        "杭州的天气怎么样？",
        session
    )
    
    print(f"位置: {weather_data.location}")
    print(f"温度: {weather_data.temperature}°F")
    print(f"天气: {weather_data.condition}")
    print(f"湿度: {weather_data.humidity}%")


    # 请求数学推理的结构化输出（覆盖输出类型）
    reply = await agent.chat("我该如何解决 8x + 7 = -23", session, output_type=MathReasoning) # 为此调用覆盖 output_type
    for index, step in enumerate(reply.steps):
        print(f"第 {index + 1} 步: {step.explanation} => 输出: {step.output}")
    print("最终答案:", reply.final_answer)

if __name__ == "__main__":
    asyncio.run(get_structured_response())
```

### 代理作为工具模式

```python
import asyncio
from xagent.core import Agent, Session
from xagent.db import MessageDB
from xagent.tools import web_search

async def agent_as_tool_example():
    # 创建专业代理
    researcher_agent = Agent(
        name="research_specialist",
        system_prompt="Research expert. Gather information and provide insights.",
        model="gpt-4.1-mini",
        tools=[web_search]
    )
    
    # 将代理转换为工具
    message_db = MessageDB()
    research_tool = researcher_agent.as_tool(
        name="researcher",
        description="Research topics and provide detailed analysis",
        message_db=message_db
    )
    
    # 具有专家工具的主协调代理
    coordinator = Agent(
        name="coordinator",
        tools=[research_tool],
        system_prompt="Coordination agent that delegates to specialists.",
        model="gpt-4.1"
    )
    
    session = Session(user_id="user123")
    
    # 复杂的多步骤任务
    response = await coordinator.chat(
        "研究可再生能源的好处并写一份简要总结",
        session
    )
    print(response)

asyncio.run(agent_as_tool_example())
```

### 使用 Redis 持久化会话

```python
import asyncio
from xagent.core import Agent, Session
from xagent.db import MessageDB

async def chat_with_persistence():
    # 初始化基于 Redis 的消息存储
    message_db = MessageDB()
    
    # 创建代理
    agent = Agent(
        name="persistent_agent",
        model="gpt-4.1-mini"
    )

    # 创建具有 Redis 持久化的会话
    session = Session(
        user_id="user123", 
        session_id="persistent_session",
        message_db=message_db
    )

    # 聊天，自动保存消息
    response = await agent.chat("记住这件事：我最喜欢的颜色是蓝色", session)
    print(response)
    
    # 后续对话 - 上下文保存在 Redis 中
    response = await agent.chat("我最喜欢的颜色是什么？", session)
    print(response)

asyncio.run(chat_with_persistence())
```

## 🏗️ 架构

**现代设计，性能卓越**

```
xAgent/
├── 🤖 xagent/                # 核心异步代理框架
│   ├── __init__.py           # 包初始化和导出
│   ├── __version__.py        # 版本信息
│   ├── core/                 # 代理和会话管理
│   │   ├── __init__.py       # 核心导出（Agent、Session、HTTPAgentServer）
│   │   ├── agent.py          # 主要代理类及聊天
│   │   ├── session.py        # 会话管理及操作
│   │   ├── server.py         # 独立的 HTTP 代理服务器
│   │   ├── cli.py            # 命令行界面
│   │   └── base.py           # 基类和工具
│   ├── db/                   # 数据库层（Redis）
│   │   ├── __init__.py       # 数据库导出
│   │   └── message.py        # 消息持久化
│   ├── schemas/              # 数据模型和类型（Pydantic）
│   │   ├── __init__.py       # 架构导出
│   │   └── message.py        # 消息和工具调用模型
│   ├── tools/                # 工具生态系统
│   │   ├── __init__.py       # 工具注册（web_search、draw_image）
│   │   ├── openai_tool.py    # OpenAI 工具集成
│   │   └── mcp_demo/         # MCP 演示服务器和客户端
│   ├── utils/                # 工具函数
│   ├── multi/                # 多代理支持
│   │   ├── __init__.py       # 多代理导出
│   │   ├── swarm.py          # 代理群体协调
│   │   └── workflow.py       # 工作流管理
│   └── frontend/             # Web界面组件
│       ├── app.py            # Streamlit 聊天应用
│       └── launcher.py       # Web 界面启动器
├── 🛠️ toolkit/               # 自定义工具生态系统
│   ├── __init__.py           # 工具包注册
│   ├── tools.py              # 自定义工具（char_count）
│   ├── mcp_server.py         # 主 MCP 服务器
├── ⚙️ config/                # 配置文件
│   ├── agent.yaml            # 代理服务器配置
│   └── sub_agents_example/   # 子代理配置示例
├── 📝 examples/              # 使用示例和演示
├── 🧪 tests/                 # 完整的测试套件
├── 📁 logs/                  # 日志文件
```

## 🤖 API 参考

### 核心类

#### 🤖 Agent

主要的 AI 代理类，用于处理对话和工具执行。

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

**关键方法:**
- `async chat(user_message, session, **kwargs) -> str | BaseModel`: 主要聊天接口
- `async __call__(user_message, session, **kwargs) -> str | BaseModel`: 聊天的简写
- `as_tool(name, description, message_db) -> Callable`: 将代理转换为工具

**参数:**
- `name`: 代理标识符（默认："default_agent"）
- `system_prompt`: 代理行为的指令
- `model`: 使用的 OpenAI 模型（默认："gpt-4.1-mini"）
- `client`: 自定义的 AsyncOpenAI 客户端实例
- `tools`: 函数工具的列表
- `mcp_servers`: 动态工具加载的 MCP 服务器 URL
- `sub_agents`: 子代理配置的列表（名称、描述、服务器 URL）

#### 💬 Session

管理对话历史和持久性及操作。

```python
Session(
    user_id: str,
    session_id: Optional[str] = None,
    message_db: Optional[MessageDB] = None
)
```

**关键方法:**
- `async add_messages(messages: Message | List[Message]) -> None`: 存储消息
- `async get_messages(count: int = 20) -> List[Message]`: 检索消息历史
- `async clear_session() -> None`: 清除对话历史
- `async pop_message() -> Optional[Message]`: 移除最后一条非工具消息

#### 🗄️ MessageDB

基于 Redis 的消息持久化层。

```python
# 使用环境变量或默认值初始化
message_db = MessageDB()

# 与会话一起使用
session = Session(
    user_id="user123",
    message_db=message_db
)
```

### 重要注意事项

| 方面 | 细节 |
|--------|---------|
| **工具函数** | 可以是同步或异步（自动转换） |
| **代理交互** | 始终使用 `await` |
| **上下文** | 在上下文中运行，使用 `asyncio.run()` |
| **并发** | 所有工具自动并行执行 |

## 📊 监控与可观测性

xAgent 包含全面的可观测性特性：

- **🔍 Langfuse 集成** - 跟踪 AI 交互和性能
- **📝 结构化日志** - 整个系统
- **❤️ 健康检查** - API 监控端点
- **⚡ 性能指标** - 工具执行时间和成功率

## 🤝 贡献

我们欢迎贡献！以下是如何开始：

### 开发工作流程

1. **Fork** 代码库
2. **创建** 功能分支： `git checkout -b feature/amazing-feature`
3. **提交** 更改： `git commit -m 'Add amazing feature'`
4. **推送** 到分支： `git push origin feature/amazing-feature`
5. **打开** 拉取请求

### 开发指南

| 领域 | 要求 |
|------|-------------|
| **代码风格** | 遵循 PEP 8 标准 |
| **测试** | 为新功能添加测试 |
| **文档** | 根据需要更新文档 |
| **类型安全** | 在整个代码中使用类型提示 |
| **提交** | 遵循传统的提交信息格式 |

## 包上传

首次上传

```bash
pip install build twine
python -m build
twine upload dist/*
```

后续上传

```bash
rm -rf dist/ build/ *.egg-info/
python -m build
twine upload dist/*
```

## 📄 许可证

本项目根据 **MIT 许可证** 进行许可 - 请参阅 [LICENSE](LICENSE) 文件了解详细信息。

## 🙏 致谢

特别感谢那些让 xAgent 成为可能的开源项目：

- **[OpenAI](https://openai.com/)** - 驱动我们 AI 的 GPT 模型
- **[FastAPI](https://fastapi.tiangolo.com/)** - 强大的异步 API 框架
- **[Streamlit](https://streamlit.io/)** - 直观的 Web界面
- **[Redis](https://redis.io/)** - 高性能数据存储
- **[Langfuse](https://langfuse.com/)** - 可观测性和监控

## 📞 支持与社区

| 资源 | 链接 | 目的 |
|----------|------|---------|
| **🐛 问题** | [GitHub Issues](https://github.com/ZJCODE/xAgent/issues) | 错误报告和功能请求 |
| **💬 讨论** | [GitHub Discussions](https://github.com/ZJCODE/xAgent/discussions) | 社区聊天和问答 |
| **📧 邮件** | zhangjun310@live.com | 直接支持 |

---

<div align="center">

**xAgent** - 赋能 AI 对话 🚀

[![GitHub stars](https://img.shields.io/github/stars/ZJCODE/xAgent?style=social)](https://github.com/ZJCODE/xAgent)
[![GitHub forks](https://img.shields.io/github/forks/ZJCODE/xAgent?style=social)](https://github.com/ZJCODE/xAgent)

*为 AI 社区而生，倾注我们的 ❤️*

</div>
</div>
