# xAgent CLI 使用指南

xAgent CLI 是一个命令行界面工具，让你可以通过终端与 xAgent 进行交互。

## 安装

```bash
pip install -e .
```

## 基本用法

### 1. 交互式聊天模式（默认）

启动交互式聊天会话：

```bash
# 默认配置
xagent-cli

# 或者明确指定 chat 命令
xagent-cli chat

# 使用自定义配置
xagent-cli chat --config config/agent.yaml --user_id user123
```

在交互模式中，你可以使用以下命令：
- `exit`, `quit`, `bye` - 退出聊天会话
- `clear` - 清除会话历史
- `help` - 显示帮助信息

### 2. 单次问答模式

发送单个问题并获得回答：

```bash
# 基本用法
xagent-cli ask "What is the capital of France?"

# 使用自定义参数
xagent-cli ask "Tell me a joke" --user_id user123 --session_id session456

# 使用自定义配置文件
xagent-cli ask "How are you?" --config config/agent.yaml
```

## 命令行参数

### 全局参数

- `--config`: 配置文件路径（默认: `config/agent.yaml`）
- `--toolkit_path`: 工具包目录路径（默认: `toolkit`）
- `--user_id`: 用户 ID（如未指定将自动生成）
- `--session_id`: 会话 ID（如未指定将自动生成）

### 子命令

#### `chat` - 交互式聊天

启动一个持续的聊天会话，允许多轮对话。

```bash
xagent-cli chat [options]
```

#### `ask` - 单次问答

发送单个消息并获得回答后退出。

```bash
xagent-cli ask <message> [options]
```

## 示例

### 交互式聊天示例

```bash
$ xagent-cli chat
🤖 Welcome to xAgent CLI!
Agent: Agent
Model: gpt-4.1-mini
Tools: 3 loaded
Session: cli_session_abc123
Type 'exit', 'quit', or 'bye' to end the session.
Type 'clear' to clear the session history.
Type 'help' for available commands.
--------------------------------------------------

👤 You: Hello, how are you?
🤖 Agent: Hello! I'm doing well, thank you for asking. I'm here and ready to help you with any questions or tasks you might have. How can I assist you today?

👤 You: What's the weather like?
🤖 Agent: I don't have access to real-time weather data, but I can help you find weather information if you tell me your location, or I can suggest ways to check the weather yourself.

👤 You: help
📋 Available commands:
  exit, quit, bye  - Exit the chat session
  clear           - Clear session history
  help            - Show this help message

🔧 Available tools:
  - web_search
  - draw_image
  - char_count

👤 You: exit
👋 Goodbye!
```

### 单次问答示例

```bash
$ xagent-cli ask "What is 2+2?"
2 + 2 equals 4.

$ xagent-cli ask "Tell me about Python programming" --user_id developer
Python is a high-level, interpreted programming language known for its simplicity and readability...
```

## 配置

CLI 使用与 xAgent 服务器相同的配置文件 `config/agent.yaml`。确保你的配置文件包含：

```yaml
agent:
  name: "Agent"
  system_prompt: |
    You are a helpful assistant...
  model: "gpt-4.1-mini"
  tools:
    - "web_search"
    - "draw_image"
  use_local_session: false
```

## 环境变量

确保设置必要的环境变量：

```bash
export OPENAI_API_KEY="your-openai-api-key"
export LANGFUSE_PUBLIC_KEY="your-langfuse-public-key"  # 可选
export LANGFUSE_SECRET_KEY="your-langfuse-secret-key"  # 可选
```

## 故障排除

### 常见问题

1. **配置文件未找到**
   ```
   FileNotFoundError: Cannot find config file...
   ```
   确保配置文件存在或使用 `--config` 参数指定正确路径。

2. **MCP 服务器连接失败**
   ```
   Failed to get tools from MCP server...
   ```
   这通常是正常的，如果 MCP 服务器未运行，CLI 将使用内置工具。

3. **API 密钥错误**
   ```
   AuthenticationError...
   ```
   检查你的 `OPENAI_API_KEY` 环境变量是否正确设置。

### 调试模式

要获得更详细的日志信息，可以设置环境变量：

```bash
export LOG_LEVEL=DEBUG
xagent-cli ask "test message"
```

## 与服务器模式的比较

| 特性 | CLI 模式 | 服务器模式 |
|------|----------|------------|
| 接口 | 命令行 | HTTP API |
| 会话管理 | 本地/数据库 | 数据库 |
| 适用场景 | 开发、测试、快速查询 | 生产、集成、Web应用 |
| 启动方式 | `xagent-cli` | `xagent-server` |

## 编程式使用

你也可以在 Python 代码中直接使用 CLI 组件：

```python
from xagent.core.cli import CLIAgent
import asyncio

async def main():
    cli_agent = CLIAgent(config_path="config/agent.yaml")
    response = await cli_agent.chat_single("Hello, world!")
    print(response)

asyncio.run(main())
```
