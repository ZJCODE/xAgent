# 性能优化

## 1. 关键性能瓶颈分析

### 1.1 每次对话都触发 MCP 服务器注册

**问题代码**（`agent.py` 的 `chat()` 方法）：

```python
async def chat(self, user_message: str, ...) -> ...:
    # 每次 chat() 调用都进入此方法
    await self._register_mcp_servers(self.mcp_servers)
    ...
```

即使缓存未过期，`_register_mcp_servers` 仍会被调用并进行时间戳比较。虽然这个操作本身很快，但在高频场景下（每秒数百次请求）会造成不必要的函数调用开销和潜在的锁竞争。

**改进方案**：

```python
async def chat(self, user_message: str, ...) -> ...:
    # 仅在 MCP 工具缓存过期时才刷新
    if self._should_refresh_mcp_tools():
        await self._register_mcp_servers(self.mcp_servers)
    ...

def _should_refresh_mcp_tools(self) -> bool:
    """Check if MCP tools need refreshing without entering async context."""
    if not self.mcp_servers:
        return False
    if self.mcp_tools_last_updated is None:
        return True
    return (time.monotonic() - self.mcp_tools_last_updated) >= self.mcp_cache_ttl
```

注意：使用 `time.monotonic()` 替代 `time.time()` 以避免系统时钟回拨问题。

### 1.2 `input_messages` 在工具调用循环中不断增长

**问题代码**：

```python
async def chat(self, ...):
    input_messages = [msg.to_dict() for msg in await self.message_storage.get_messages(...)]
    
    for attempt in range(max_iter):
        reply_type, response = await self._call_model(input_messages, ...)
        
        elif reply_type == ReplyType.TOOL_CALL:
            # 每次工具调用都向 input_messages 追加消息
            await self._handle_tool_calls(response, user_id, session_id, input_messages, ...)
            # ↑ input_messages 会无限增长，每次迭代都更长
```

在多工具调用场景中（`max_iter=10`），`input_messages` 可能累积大量 function_call 和 function_call_output 消息，导致：
- Token 消耗指数增长
- 消息序列化时间增加
- API 请求大小超限

**改进方案**：

```python
async def chat(self, ...):
    # 从存储加载历史，这是不可变的基础
    base_messages = [msg.to_dict() for msg in await self.message_storage.get_messages(...)]
    
    # 工具调用产生的临时消息，本次对话结束后丢弃
    tool_exchange_messages: List[dict] = []
    
    for attempt in range(max_iter):
        # 动态组合：历史 + 本轮工具交换
        current_messages = base_messages + tool_exchange_messages
        reply_type, response = await self._call_model(current_messages, ...)
        
        if reply_type == ReplyType.TOOL_CALL:
            new_messages = await self._handle_tool_calls(response, ...)
            tool_exchange_messages.extend(new_messages)
```

### 1.3 记忆提取的多次串行 LLM 调用

**问题**：记忆存储流程进行多次串行 LLM 调用：
1. `extract_memories_from_content()` → LLM 调用 #1
2. `vector_store.query()` → 向量检索
3. `merge_memories()` → LLM 调用 #2

当消息量大时，这两次 LLM 调用串行执行，总延迟 = T1 + T2。

**改进方案**：并行化独立操作：

```python
async def store(self, user_id: str, content: str) -> str:
    # 并发执行内容提取（需要LLM）和历史向量查询（不需要LLM）
    # 但注意：提取需要先完成才能查询，所以这里改为优化批量操作
    
    extracted_memories = await self.llm_service.extract_memories_from_content(content)
    
    if not extracted_memories.memories:
        return ""
    
    # 向量查询和合并可以并行处理，用锁避免重复删除
    query_texts = [m.content for m in extracted_memories.memories]
    
    # 并行：向量查询（已经是批量的）
    related_vector_docs = await self.vector_store.query(
        query_texts=query_texts, n_results=2, meta_filter={"user_id": user_id}
    )
    
    # 当没有相关记忆时，跳过昂贵的合并LLM调用
    if not related_vector_docs:
        return await self._store_memories_directly(user_id, extracted_memories.memories)
    
    # 有相关记忆时，才执行合并
    merged_result = await self.llm_service.merge_memories(
        extracted_memories=extracted_memories,
        related_memories=self._process_vector_docs(related_vector_docs)
    )
    return await self._store_memories_directly(user_id, merged_result.memories)
```

---

## 2. 工具调用性能优化

### 2.1 工具调用超时机制缺失

**问题**：工具函数执行时没有超时控制，一个慢工具会阻塞整个 Agent 循环：

```python
# 当前（无超时）
async def execute_with_semaphore(tool_call):
    async with semaphore:
        return await self._act(tool_call, user_id, session_id)
```

**改进方案**：

```python
class AgentConfig:
    DEFAULT_TOOL_TIMEOUT = 30.0    # 单个工具最大执行时间（秒）
    DEFAULT_TOOL_RETRY_TIMES = 2   # 工具失败重试次数

async def _execute_tool_with_timeout(
    self, 
    tool_call, 
    user_id: str, 
    session_id: str,
    timeout: float = AgentConfig.DEFAULT_TOOL_TIMEOUT
) -> Optional[list]:
    """Execute a tool call with timeout and retry."""
    try:
        return await asyncio.wait_for(
            self._act(tool_call, user_id, session_id),
            timeout=timeout
        )
    except asyncio.TimeoutError:
        name = getattr(tool_call, "name", "unknown")
        self.logger.warning("Tool '%s' timed out after %.1fs", name, timeout)
        # 返回超时消息而非抛出异常，让 Agent 继续运行
        return self._create_timeout_messages(tool_call, timeout)

def _create_timeout_messages(self, tool_call, timeout: float) -> list:
    """Create error messages for a timed-out tool call."""
    name = getattr(tool_call, "name", "unknown")
    return [
        Message(
            type=MessageType.FUNCTION_CALL,
            role=RoleType.TOOL,
            content=f"Calling tool: `{name}`",
            tool_call=ToolCall(call_id=getattr(tool_call, "call_id", ""), name=name, arguments="{}")
        ),
        Message(
            type=MessageType.FUNCTION_CALL_OUTPUT,
            role=RoleType.TOOL,
            content=f"Tool `{name}` result: timeout",
            tool_call=ToolCall(
                call_id=getattr(tool_call, "call_id", "001"),
                output=f"Tool execution timed out after {timeout}s. Please try a simpler approach."
            )
        )
    ]
```

### 2.2 工具规格缓存竞争条件

**问题**：`cached_tool_specs` 属性在并发场景下可能多次重建缓存：

```python
@property
def cached_tool_specs(self):
    if self._should_rebuild_cache():  # 线程A和线程B同时到达这里
        self._rebuild_tool_cache()    # 两个都执行重建
    return self._tool_specs_cache
```

**改进方案**：使用锁保护缓存重建：

```python
import threading

class Agent:
    def __init__(self, ...):
        self._cache_lock = asyncio.Lock()  # 异步锁
    
    @property
    def cached_tool_specs(self):
        """Synchronous property for backward compatibility."""
        if self._should_rebuild_cache():
            self._rebuild_tool_cache()
        return self._tool_specs_cache
    
    async def _get_tool_specs_async(self) -> Optional[list]:
        """Async-safe tool spec retrieval with lock protection."""
        if self._should_rebuild_cache():
            async with self._cache_lock:
                # Double-check after acquiring lock
                if self._should_rebuild_cache():
                    self._rebuild_tool_cache()
        return self._tool_specs_cache
```

---

## 3. 内存优化

### 3.1 消息序列化开销

**问题**：每次 `chat()` 都对所有历史消息执行 `to_dict()` 序列化：

```python
input_messages = [msg.to_dict() for msg in await self.message_storage.get_messages(...)]
```

当历史消息较多时（`history_count=16`），这会产生大量重复序列化。

**改进方案**：在存储层缓存序列化结果：

```python
class MessageStorageLocal:
    """Message storage with serialization cache."""
    
    async def get_messages_dict(
        self, 
        user_id: str, 
        session_id: str, 
        count: int
    ) -> List[dict]:
        """Return messages already serialized to dict format."""
        messages = await self.get_messages(user_id, session_id, count)
        # 对于 Redis 存储，消息已经是 JSON 格式，无需重复序列化
        return [msg.to_dict() for msg in messages]
```

### 3.2 向量查询 `n_results=1000` 的性能问题

**问题代码**（`basic_memory.py`）：

```python
# 获取所有记忆时使用极大的 n_results
vector_docs = await self.vector_store.query(
    query_texts=[""],
    n_results=1000,    # ← 可能加载大量数据到内存
    meta_filter=meta_filter
)
```

**改进方案**：使用分页查询：

```python
async def _get_recent_memories_paginated(
    self, 
    user_id: str, 
    days: int = 1,
    page_size: int = 100
) -> List[Dict[str, Any]]:
    """Get recent memories with pagination to avoid memory overload."""
    all_memories = []
    offset = 0
    
    while True:
        batch = await self.vector_store.query(
            query_texts=[""],
            n_results=page_size,
            offset=offset,
            meta_filter=self._build_time_filter(user_id, days)
        )
        
        if not batch:
            break
            
        all_memories.extend(batch)
        
        if len(batch) < page_size:
            break
            
        offset += page_size
    
    return all_memories
```

---

## 4. 并发模型优化

### 4.1 Parallel Workflow 的 Semaphore 位置

**问题**：`ParallelWorkflow` 在每次执行时创建新的 `Semaphore`，这是正确的。但 `GraphWorkflow` 在每个执行层也创建新 `Semaphore`：

```python
# GraphWorkflow.execute() 中
for layer_idx, layer_agents in enumerate(execution_layers):
    semaphore = asyncio.Semaphore(self.max_concurrent)  # ← 每层创建新 Semaphore
```

这意味着跨层的并发总量没有限制。如果有 5 层每层 10 个 agent，最大并发可能是 50。

**改进方案**：在执行级别而不是层级别创建 Semaphore：

```python
async def execute(self, user_id: str, task: str, ...) -> WorkflowResult:
    # 全局并发限制，贯穿整个图的执行
    global_semaphore = asyncio.Semaphore(self.max_concurrent)
    
    for layer_idx, layer_agents in enumerate(execution_layers):
        layer_tasks = [
            self._execute_with_semaphore(global_semaphore, agent, input_text, user_id)
            for agent, input_text in zip(layer_agent_objects, layer_inputs)
        ]
        layer_task_results = await asyncio.gather(*layer_tasks)
```

### 4.2 连接池配置

**问题**：`AsyncOpenAI` 客户端使用默认连接池配置，在高并发场景下可能成为瓶颈。

**改进方案**：

```python
import httpx
from langfuse.openai import AsyncOpenAI

class Agent:
    def __init__(self, ..., http_client: Optional[httpx.AsyncClient] = None):
        # 允许传入自定义 HTTP 客户端以控制连接池
        if http_client is None:
            http_client = httpx.AsyncClient(
                limits=httpx.Limits(
                    max_connections=100,        # 最大连接数
                    max_keepalive_connections=20,  # 保持活跃的连接数
                    keepalive_expiry=30,         # 连接保活时间（秒）
                ),
                timeout=httpx.Timeout(
                    connect=10.0,   # 连接超时
                    read=600.0,     # 读取超时
                    write=30.0,     # 写入超时
                    pool=30.0,      # 从连接池获取连接的超时
                )
            )
        
        self.client = client or AsyncOpenAI(http_client=http_client)
```

---

## 5. 缓存策略

### 5.1 系统提示格式化缓存

**问题**：每次 `_call_model` 调用都对系统提示进行字符串格式化：

```python
system_msg = {
    "role": "system",
    "content": self.system_prompt.format(
        user_id=user_id, 
        date=time.strftime('%Y-%m-%d'),  # ← 每次调用都格式化
        timezone=time.tzname[0],
        retrieved_memories=retrieved_memories or "No relevant memories found.",
        shared_context=shared_context or "No shared context."
    )
}
```

当 `retrieved_memories` 和 `shared_context` 没有变化时，这是冗余计算。

**改进方案**：

```python
# 对不变的部分预计算
_DATE_FORMAT = '%Y-%m-%d'
_TIMEZONE = time.tzname[0]  # 进程启动时固定

def _build_system_message(
    self, 
    user_id: str,
    retrieved_memories: Optional[List],
    shared_context: Optional[str]
) -> dict:
    return {
        "role": "system",
        "content": self.system_prompt.format(
            user_id=user_id,
            date=time.strftime(_DATE_FORMAT),  # 日期每天变一次，可以按天缓存
            timezone=_TIMEZONE,
            retrieved_memories=retrieved_memories or "No relevant memories found.",
            shared_context=shared_context or "No shared context."
        )
    }
```

### 5.2 LRU 缓存用于常量查询

对于经常重复的只读查询（如工具规格、模型信息），可以使用 `functools.lru_cache`：

```python
from functools import lru_cache

class Agent:
    @lru_cache(maxsize=1)
    def _get_static_tool_count(self) -> int:
        """Cache the count of statically registered tools."""
        return len(self.tools)
```

---

## 6. 基准测试建议

建议建立以下性能基准测试，以量化优化效果：

```python
# tests/benchmarks/test_agent_performance.py

import time
import asyncio
import pytest
from xagent import Agent

@pytest.mark.benchmark
async def test_chat_throughput(benchmark_agent, mock_openai):
    """Measure single-agent chat requests per second."""
    agent = benchmark_agent
    
    start = time.monotonic()
    tasks = [agent.chat(f"Query {i}") for i in range(100)]
    results = await asyncio.gather(*tasks)
    elapsed = time.monotonic() - start
    
    rps = 100 / elapsed
    print(f"Throughput: {rps:.1f} requests/second")
    assert rps > 50  # 目标：>50 RPS

@pytest.mark.benchmark
async def test_tool_call_latency(benchmark_agent, mock_tool):
    """Measure tool call round-trip latency."""
    agent = benchmark_agent
    
    latencies = []
    for _ in range(20):
        start = time.monotonic()
        await agent.chat("Use the tool to calculate 2+2")
        latencies.append(time.monotonic() - start)
    
    p95 = sorted(latencies)[int(len(latencies) * 0.95)]
    print(f"Tool call P95 latency: {p95*1000:.0f}ms")
    assert p95 < 2.0  # 目标：P95 < 2 秒（包含模型调用时间）

@pytest.mark.benchmark
async def test_memory_retrieval_latency(memory_agent):
    """Measure memory retrieval overhead."""
    start = time.monotonic()
    memories = await memory_agent.memory_storage.retrieve(
        user_id="test_user",
        query="recent activities",
        limit=5
    )
    latency = time.monotonic() - start
    
    print(f"Memory retrieval latency: {latency*1000:.0f}ms")
    assert latency < 0.5  # 目标：< 500ms
```

---

## 7. 性能优化优先级汇总

| 优化项 | 预期收益 | 实施难度 | 优先级 |
|--------|----------|----------|--------|
| 工具调用超时 | 防止阻塞、提升稳定性 | 低 | P1 |
| input_messages 累积问题 | 减少 token 消耗 | 低 | P1 |
| MCP 缓存检查优化 | 减少函数调用开销 | 低 | P2 |
| 记忆存储跳过无意义合并 | 减少 50% LLM 调用 | 中 | P2 |
| HTTP 客户端连接池 | 提升并发性能 | 中 | P2 |
| 全局 Semaphore for Graph | 更准确的并发控制 | 低 | P2 |
| 记忆分页查询 | 防止内存溢出 | 中 | P3 |
| 缓存锁保护 | 防止并发问题 | 低 | P3 |
