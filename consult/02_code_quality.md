# 代码质量与工程规范

## 1. 代码规范问题

### 1.1 中英文注释混用

项目中存在大量中文注释，在开源项目中会显著降低国际化贡献者的参与意愿：

```python
# 当前代码（agent.py）
# 使用智能缓存的工具规格
tool_specs = self.cached_tool_specs

# 预处理消息
messages = [system_msg] + self._sanitize_input_messages(input_msgs)

# 根据是否需要结构化输出选择不同的API调用,结构化输出强制不使用Stream 模式
if output_type is not None:

# 注册新工具后使缓存失效
self._tool_specs_cache = None
```

**改进方案**：统一使用英文注释，符合 PEP 8 规范：

```python
# Use cached tool specifications for efficiency
tool_specs = self.cached_tool_specs

# Prepend system message to conversation history
messages = [system_msg] + self._sanitize_input_messages(input_msgs)

# Structured output requires non-streaming responses
if output_type is not None:

# Invalidate cache when new tools are registered
self._tool_specs_cache = None
```

### 1.2 类型注解不完整

部分方法缺少完整的类型注解：

```python
# 当前（不完整）
async def _call_model(self, input_msgs: list, 
                      user_id: str, session_id: str,
                      output_type: type[BaseModel] = None,  # ← 应该是 Optional[type[BaseModel]]
                      stream: bool = False,
                      retrieved_memories: Optional[List[dict]] = None,
                      shared_context: Optional[str] = None
                      ) -> tuple[ReplyType, object]:  # ← object 太宽泛
```

**改进方案**：

```python
from typing import Any, Tuple

async def _call_model(
    self, 
    input_msgs: List[dict], 
    user_id: str, 
    session_id: str,
    output_type: Optional[Type[BaseModel]] = None,
    stream: bool = False,
    retrieved_memories: Optional[List[Dict[str, Any]]] = None,
    shared_context: Optional[str] = None,
) -> Tuple[ReplyType, Union[str, BaseModel, AsyncGenerator[str, None]]]:
```

### 1.3 魔法数字

代码中存在硬编码的魔法数字，含义不明：

```python
# 当前（不清晰）
pre_chat = messages_without_tool[-3:]  # 为什么是 3？
limit=5                                # 为什么是 5？
self.SHARED_HISTORY_COUNT = 10         # 为什么是 10？
TOOL_RESULT_PREVIEW_LENGTH = 20        # 为什么是 20？
```

**改进方案**：为常量添加清晰的名称和注释：

```python
class AgentConfig:
    # Number of recent messages to use as context for memory retrieval
    MEMORY_RETRIEVAL_CONTEXT_MESSAGES = 3
    
    # Maximum number of memories to retrieve per query
    DEFAULT_MEMORY_RETRIEVAL_LIMIT = 5
    
    # Number of recent shared messages to track for group context
    DEFAULT_SHARED_HISTORY_COUNT = 10
    
    # Max characters to show in tool result preview logs
    TOOL_RESULT_PREVIEW_LENGTH = 20
```

---

## 2. 函数设计问题

### 2.1 `_sanitize_input_messages` 就地修改

`_sanitize_input_messages` 方法就地修改传入的列表，产生不可预期的副作用：

```python
# 当前实现（有副作用）
@staticmethod
def _sanitize_input_messages(input_messages: list) -> list:
    while input_messages and input_messages[0].get("type") == MessageType.FUNCTION_CALL_OUTPUT:
        input_messages.pop(0)  # ← 就地修改，改变了调用者的数据！
    return input_messages
```

这会导致以下问题：
- 每次 `chat()` 迭代后，`input_messages` 变量被意外修改
- 调试困难，因为状态被隐式改变
- 破坏函数式编程原则

**改进方案**：

```python
@staticmethod
def _sanitize_input_messages(input_messages: List[dict]) -> List[dict]:
    """Return a copy of input_messages with leading function_call_output items removed."""
    start_idx = 0
    while (
        start_idx < len(input_messages) 
        and input_messages[start_idx].get("type") == MessageType.FUNCTION_CALL_OUTPUT.value
    ):
        start_idx += 1
    return input_messages[start_idx:]  # 返回切片副本，不修改原始数据
```

### 2.2 `chat()` 方法过长（~130 行）

`chat()` 方法包含太多逻辑，难以理解和测试。建议提取子方法：

```python
async def chat(self, user_message: str, ...) -> Union[str, BaseModel, ...]:
    """Simplified main chat method."""
    # 1. 准备阶段
    session_id = self._build_session_id(session_id)
    output_type = output_type or self.output_type
    if output_type:
        stream = False
    
    # 2. 刷新工具
    await self._register_mcp_servers(self.mcp_servers)
    
    # 3. 获取对话上下文
    context = await self._build_conversation_context(
        user_message, user_id, session_id, history_count, 
        image_source, enable_memory, shared
    )
    
    # 4. 推理循环
    return await self._reasoning_loop(
        context, user_id, session_id, 
        output_type, stream, max_iter, max_concurrent_tools
    )

async def _build_conversation_context(self, ...) -> ConversationContext:
    """Build all context needed for a chat turn."""
    ...

async def _reasoning_loop(self, context: ConversationContext, ...) -> Any:
    """Execute the agent reasoning loop."""
    ...
```

### 2.3 流式响应中的脆弱事件跳过逻辑

```python
# 当前实现（脆弱）
# Get the third event to determine the stream type
await anext(response, None)  # Skip first event
await anext(response, None)  # Skip second event
third_event = await anext(response, None)
event_type = third_event.item.type if third_event else None
```

这种通过跳过固定数量事件来判断响应类型的方式非常脆弱：
- 依赖 OpenAI API 的事件顺序不变（不保证）
- 跳过的事件中可能包含有用信息
- `third_event.item.type` 在结构变化时会抛出 `AttributeError`

**改进方案**：

```python
async def _handle_stream_response(
    self,
    response: AsyncGenerator,
    user_id: str,
    session_id: str,
    shared_context: Optional[str],
) -> Tuple[ReplyType, Any]:
    """Handle streaming response by inspecting event types safely."""
    
    collected_events = []
    
    # Collect events and detect type without discarding them
    async for event in response:
        collected_events.append(event)
        
        # Determine response type from first substantive event
        event_type = getattr(getattr(event, 'item', None), 'type', None)
        if event_type in ("message", "function_call"):
            break
    
    if not collected_events:
        return ReplyType.ERROR, _empty_stream_generator()
    
    detected_type = getattr(getattr(collected_events[-1], 'item', None), 'type', None)
    
    if detected_type == "message":
        return ReplyType.SIMPLE_REPLY, self._create_text_stream(
            collected_events, response, user_id, session_id, shared_context
        )
    elif detected_type == "function_call":
        # Drain remaining stream and return tool calls
        async for event in response:
            collected_events.append(event)
        return ReplyType.TOOL_CALL, collected_events[-1].response.output
    
    return ReplyType.ERROR, _empty_stream_generator()
```

---

## 3. 资源管理问题

### 3.1 未被等待的异步任务

`asyncio.create_task()` 创建的任务没有被存储或等待，如果任务失败，错误会被静默忽略：

```python
# 当前（危险）
asyncio.create_task(self.memory_storage.add(user_id=user_id, messages=messages_without_tool[-2:]))
```

**改进方案**：

```python
# 方案1：添加错误回调
task = asyncio.create_task(
    self.memory_storage.add(user_id=user_id, messages=messages_without_tool[-2:])
)
task.add_done_callback(lambda t: 
    self.logger.error("Memory storage task failed: %s", t.exception()) 
    if t.exception() else None
)

# 方案2：使用 TaskGroup（Python 3.11+，推荐）
async with asyncio.TaskGroup() as tg:
    tg.create_task(self.memory_storage.add(...))
    # 其他并行任务
```

### 3.2 HTTP 客户端未复用

`_convert_http_agent_to_tool` 中每次调用都创建新的 `httpx.AsyncClient`：

```python
# 当前（低效）
async def make_http_request():
    async with httpx.AsyncClient(timeout=AgentConfig.HTTP_TIMEOUT) as client:
        return await client.post(f"{server}/chat", json=payload)
```

**改进方案**：在 Agent 级别维护 HTTP 客户端池：

```python
class Agent:
    def __init__(self, ...):
        self._http_client: Optional[httpx.AsyncClient] = None
    
    @property
    def http_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=AgentConfig.HTTP_TIMEOUT)
        return self._http_client
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, *args):
        if self._http_client:
            await self._http_client.aclose()
```

### 3.3 MCP 客户端连接管理

每次 MCP 工具调用都重新创建客户端连接：

```python
# mcp_convertor.py 中
async def openai_tool_func(**kwargs):
    async with self.client:   # ← 每次调用都建立新连接
        result = await self.client.call_tool(name, kwargs)
```

**改进方案**：

```python
class MCPTool:
    def __init__(self, url: str):
        self._url = url
        self._client: Optional[Client] = None
    
    async def _get_client(self) -> Client:
        if self._client is None:
            self._client = Client(self._url)
            await self._client.__aenter__()
        return self._client
    
    async def close(self):
        if self._client:
            await self._client.__aexit__(None, None, None)
            self._client = None
```

---

## 4. 错误信息质量

### 4.1 错误消息对用户不友好

部分错误消息直接将技术异常暴露给用户：

```python
# 当前（暴露技术细节）
result = f"Tool error: {e}"  # 可能包含堆栈信息、文件路径等

return f"Model call error: {str(e)}"  # 可能包含 API 密钥片段
```

**改进方案**：

```python
# 区分面向用户的消息和面向日志的消息
TOOL_ERROR_USER_MESSAGE = "The tool '{name}' encountered an error. Please try again or rephrase your request."
TOOL_ERROR_LOG_TEMPLATE = "Tool '{name}' failed with exception: {error}"

try:
    result = await func(**args)
except Exception as e:
    tool_name = name or "unknown"
    self.logger.error(TOOL_ERROR_LOG_TEMPLATE.format(name=tool_name, error=e), exc_info=True)
    result = TOOL_ERROR_USER_MESSAGE.format(name=tool_name)
```

---

## 5. 文档质量

### 5.1 缺少模块级文档字符串

主要模块缺少 module-level docstring：

```python
# 建议在每个模块顶部添加
"""
xagent.core.agent
~~~~~~~~~~~~~~~~~

Core Agent implementation providing the main reasoning loop,
tool management, and conversation handling.

Usage::

    from xagent import Agent
    
    agent = Agent(
        name="assistant",
        model="gpt-4.1-mini",
        tools=[my_tool],
    )
    response = await agent.chat("Hello!")
"""
```

### 5.2 示例代码质量参差

`examples/` 目录下的示例缺少统一的错误处理和最佳实践展示：

```python
# 建议每个示例包含：
# 1. 模块文档说明这个示例演示什么
# 2. 完整的 try/except 处理
# 3. 资源清理（使用 async with）
# 4. 配置加载（使用 .env）

"""
Example: Basic Chat with Error Handling
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Demonstrates how to create a simple agent and handle common error cases.
"""

import asyncio
from dotenv import load_dotenv

async def main():
    load_dotenv()
    
    agent = Agent(name="assistant")
    
    try:
        async with agent:
            response = await agent.chat("Hello!")
            print(f"Response: {response}")
    except AgentError as e:
        print(f"Agent error: {e}")
    except Exception as e:
        print(f"Unexpected error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
```

---

## 6. 代码重复

### 6.1 HTTP Agent 与本地 Agent 工具转换代码重复

`as_tool()` 和 `_convert_http_agent_to_tool()` 方法有大量重复逻辑：

```python
# as_tool() 中
@function_tool(
    name=name or self.name,
    description=description or self.description,
    param_descriptions={
        "input": "A clear, focused instruction...",
        "expected_output": "Specification of the desired output...",
        "image_source": "Optional list of image URLs..."
    }
)
async def tool_func(input: str, expected_output: str, image_source: Optional[List[str]] = None):
    user_message = f"### User Input:\n{input}"
    if expected_output:
        user_message += f"\n\n### Expected Output:\n{expected_output}"
    ...

# _convert_http_agent_to_tool() 中（几乎相同）
@function_tool(
    name=name,
    description=description,
    param_descriptions={
        "input": "A clear, focused instruction...",  # ← 完全相同
        "expected_output": "Specification of the desired output...",  # ← 完全相同
        "image_source": "Optional list of image URLs..."  # ← 完全相同
    }
)
async def tool_func(input: str, expected_output: str, image_source: Optional[List[str]] = None):
    user_message = f"### User Input:\n{input}"  # ← 完全相同
    if expected_output:
        user_message += f"\n\n### Expected Output:\n{expected_output}"  # ← 完全相同
    ...
```

**改进方案**：提取公共逻辑：

```python
# 提取公共常量
AGENT_TOOL_PARAM_DESCRIPTIONS = {
    "input": "A clear, focused instruction or question for the agent, sufficient to complete the task independently, with any necessary resources included.",
    "expected_output": "Specification of the desired output format, structure, or content type.",
    "image_source": "Optional list of image URLs, file paths, or base64 strings to be included in the message.",
}

def _build_agent_user_message(input: str, expected_output: str) -> str:
    """Build formatted user message for agent tool calls."""
    message = f"### User Input:\n{input}"
    if expected_output:
        message += f"\n\n### Expected Output:\n{expected_output}"
    return message

# 两个方法都调用相同的辅助函数
```

---

## 7. 改进优先级汇总

| 问题 | 影响 | 修复工作量 | 优先级 |
|------|------|----------|--------|
| 中英文注释混用 | 国际贡献者体验 | 低 | P2 |
| `_sanitize_input_messages` 副作用 | 潜在 Bug | 低 | P1 |
| 未被等待的异步任务 | 静默故障 | 低 | P1 |
| 流式事件跳过逻辑 | 稳定性 | 中 | P1 |
| HTTP 客户端未复用 | 性能 | 低 | P2 |
| 魔法数字 | 可维护性 | 低 | P3 |
| 类型注解不完整 | 开发体验 | 低 | P2 |
| chat() 方法过长 | 可维护性 | 高 | P3 |
| 代码重复 | 可维护性 | 低 | P2 |
| 文档质量 | 开发者体验 | 中 | P3 |
