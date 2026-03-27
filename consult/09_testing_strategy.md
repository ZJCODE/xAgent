# 测试策略

## 1. 当前测试状态

```
tests/
├── test_redis.py                    # Redis 基础功能测试
└── test_redis_cluster_support.py    # Redis 集群测试

核心模块测试覆盖：
  xagent/core/agent.py         → ❌ 无测试
  xagent/multi/workflow.py     → ❌ 无测试
  xagent/multi/swarm.py        → ❌ 无测试
  xagent/components/memory/    → ❌ 无测试
  xagent/interfaces/server.py  → ❌ 无测试
  xagent/utils/tool_decorator.py → ❌ 无测试
  xagent/utils/workflow_dsl.py   → ❌ 无测试
```

测试覆盖率估计 < 5%，这对于一个开源框架来说是严重不足。

---

## 2. 测试策略设计

### 2.1 测试金字塔

```
         /\
        /E2E\       ← 端到端测试（少量，慢速）
       /------\
      /集成测试 \    ← 组件间交互（中量）
     /----------\
    /  单元测试   \  ← 核心逻辑（大量，快速）
   /--------------\
```

建议分配：
- **单元测试**：70%（快速、隔离、无 I/O）
- **集成测试**：20%（含 Mock API）
- **端到端测试**：10%（真实 API，可选跳过）

---

## 3. 单元测试

### 3.1 工具装饰器测试

```python
# tests/unit/test_tool_decorator.py
import pytest
import asyncio
from typing import Optional, List
from xagent.utils.tool_decorator import function_tool, python_type_to_openai_type

class TestPythonTypeToOpenAIType:
    """Tests for Python type → OpenAI schema conversion."""
    
    def test_basic_types(self):
        assert python_type_to_openai_type(str) == {"type": "string"}
        assert python_type_to_openai_type(int) == {"type": "integer"}
        assert python_type_to_openai_type(float) == {"type": "number"}
        assert python_type_to_openai_type(bool) == {"type": "boolean"}
    
    def test_optional_type(self):
        result = python_type_to_openai_type(Optional[str])
        assert result == {"type": "string"}
    
    def test_list_type(self):
        result = python_type_to_openai_type(List[str])
        assert result == {"type": "array", "items": {"type": "string"}}
    
    def test_nested_list(self):
        result = python_type_to_openai_type(List[int])
        assert result == {"type": "array", "items": {"type": "integer"}}
    
    def test_enum_type(self):
        from enum import Enum
        class Color(Enum):
            RED = "red"
            BLUE = "blue"
        
        result = python_type_to_openai_type(Color)
        assert result["type"] == "string"
        assert set(result["enum"]) == {"red", "blue"}


class TestFunctionTool:
    """Tests for function_tool decorator."""
    
    @pytest.mark.asyncio
    async def test_async_function_preserved(self):
        @function_tool()
        async def my_tool(query: str) -> str:
            """Search for something."""
            return f"Result for: {query}"
        
        result = await my_tool(query="test")
        assert result == "Result for: test"
    
    def test_tool_spec_generated(self):
        @function_tool(name="custom_name", description="Custom description")
        async def my_tool(query: str, limit: int = 5) -> str:
            pass
        
        spec = my_tool.tool_spec
        assert spec["name"] == "custom_name"
        assert spec["description"] == "Custom description"
        assert spec["type"] == "function"
        
        params = spec["parameters"]
        assert "query" in params["properties"]
        assert "limit" in params["properties"]
        assert "query" in params["required"]
        assert "limit" not in params["required"]  # Has default
    
    def test_sync_function_wrapped_as_async(self):
        @function_tool()
        def sync_tool(x: int) -> int:
            """Synchronous tool."""
            return x * 2
        
        # Should be converted to async
        assert asyncio.iscoroutinefunction(sync_tool)
    
    @pytest.mark.asyncio
    async def test_sync_tool_executes_correctly(self):
        @function_tool()
        def sync_tool(x: int) -> int:
            """Synchronous tool."""
            return x * 2
        
        result = await sync_tool(x=5)
        assert result == 10
    
    def test_param_descriptions_included(self):
        @function_tool(
            param_descriptions={"query": "The search query to execute"}
        )
        async def search(query: str) -> str:
            pass
        
        prop = search.tool_spec["parameters"]["properties"]["query"]
        assert prop.get("description") == "The search query to execute"
    
    def test_non_async_tool_raises_error_when_registered(self):
        """Tool validation in Agent._register_tools should catch sync tools."""
        # Note: function_tool itself wraps sync functions
        # but if somehow a non-async function reaches _register_tools
        # it should raise TypeError
        pass
```

### 3.2 工作流 DSL 测试

```python
# tests/unit/test_workflow_dsl.py
import pytest
from xagent.utils.workflow_dsl import parse_dependencies_dsl, validate_dsl_syntax

class TestParseDependenciesDSL:
    
    def test_simple_chain(self):
        result = parse_dependencies_dsl("A->B")
        assert result == {"B": ["A"]}
    
    def test_multi_step_chain(self):
        result = parse_dependencies_dsl("A->B->C")
        assert result == {"B": ["A"], "C": ["B"]}
    
    def test_parallel_branches(self):
        result = parse_dependencies_dsl("A->B, A->C")
        assert result == {"B": ["A"], "C": ["A"]}
    
    def test_merge_dependencies(self):
        result = parse_dependencies_dsl("A->C, B->C")
        assert set(result["C"]) == {"A", "B"}
    
    def test_multiple_deps_with_ampersand(self):
        result = parse_dependencies_dsl("A&B->C")
        assert set(result["C"]) == {"A", "B"}
    
    def test_complex_graph(self):
        result = parse_dependencies_dsl("A->B, A->C, B&C->D")
        assert result["B"] == ["A"]
        assert result["C"] == ["A"]
        assert set(result["D"]) == {"B", "C"}
    
    def test_empty_string(self):
        assert parse_dependencies_dsl("") == {}
    
    def test_whitespace_handling(self):
        result = parse_dependencies_dsl("  A -> B  ,  B -> C  ")
        assert result == {"B": ["A"], "C": ["B"]}


class TestValidateDSLSyntax:
    
    def test_valid_simple(self):
        is_valid, msg = validate_dsl_syntax("A->B")
        assert is_valid
        assert msg == ""
    
    def test_invalid_double_dash(self):
        is_valid, msg = validate_dsl_syntax("A--B")
        assert not is_valid
    
    def test_invalid_no_arrow(self):
        is_valid, msg = validate_dsl_syntax("A,B")
        assert not is_valid
    
    def test_empty_is_valid(self):
        is_valid, _ = validate_dsl_syntax("")
        assert is_valid
    
    def test_invalid_characters(self):
        is_valid, msg = validate_dsl_syntax("A->B;C->D")
        assert not is_valid
```

### 3.3 Message 模型测试

```python
# tests/unit/test_message_model.py
import pytest
from xagent.schemas.message import Message, RoleType, MessageType, ToolCall

class TestMessage:
    
    def test_create_simple_message(self):
        msg = Message.create(content="Hello", role=RoleType.USER)
        assert msg.content == "Hello"
        assert msg.role == RoleType.USER
        assert msg.type == MessageType.Message
        assert msg.multimodal is None
    
    def test_to_dict_simple(self):
        msg = Message.create(content="Hello", role=RoleType.USER)
        d = msg.to_dict()
        assert d == {"role": "user", "content": "Hello"}
    
    def test_to_dict_function_call(self):
        msg = Message(
            type=MessageType.FUNCTION_CALL,
            role=RoleType.TOOL,
            content="Calling tool",
            tool_call=ToolCall(
                call_id="call_123",
                name="web_search",
                arguments='{"query": "test"}'
            )
        )
        d = msg.to_dict()
        assert d["type"] == "function_call"
        assert d["name"] == "web_search"
        assert d["call_id"] == "call_123"
        assert "output" not in d  # None values filtered
    
    def test_to_dict_function_call_output(self):
        msg = Message(
            type=MessageType.FUNCTION_CALL_OUTPUT,
            role=RoleType.TOOL,
            content="Result",
            tool_call=ToolCall(
                call_id="call_123",
                output="search result"
            )
        )
        d = msg.to_dict()
        assert d["type"] == "function_call_output"
        assert d["output"] == "search result"
        assert "name" not in d  # None values filtered
    
    def test_timestamp_is_set(self):
        msg = Message.create(content="test")
        assert msg.timestamp > 0
```

### 3.4 Agent 核心逻辑测试

```python
# tests/unit/test_agent.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from xagent.core.agent import Agent, AgentConfig, ReplyType

@pytest.fixture
def mock_openai_client():
    """Mock OpenAI client for testing without API calls."""
    client = AsyncMock()
    
    # Default: return a simple text response
    mock_response = MagicMock()
    mock_response.output_text = "Test response"
    mock_response.output = None
    
    client.responses.create = AsyncMock(return_value=mock_response)
    client.responses.parse = AsyncMock(return_value=mock_response)
    
    return client

@pytest.fixture
async def agent(mock_openai_client):
    """Create a test agent with mocked dependencies."""
    return Agent(
        name="test_agent",
        model="gpt-4.1-mini",
        client=mock_openai_client
    )

class TestAgentConfig:
    
    def test_default_session_id_format(self):
        agent = Agent(name="my_agent", client=AsyncMock())
        # Session ID should be prefixed with agent name
        # This is tested indirectly through chat behavior
        assert agent.name == "my_agent"
    
    def test_mcp_servers_normalization(self):
        agent = Agent(client=AsyncMock(), mcp_servers="http://example.com/mcp")
        assert agent.mcp_servers == ["http://example.com/mcp"]
        
        agent2 = Agent(client=AsyncMock(), mcp_servers=["http://a.com", "http://b.com"])
        assert len(agent2.mcp_servers) == 2
        
        agent3 = Agent(client=AsyncMock())
        assert agent3.mcp_servers == []


class TestAgentToolRegistration:
    
    def test_register_valid_tool(self):
        from xagent.utils.tool_decorator import function_tool
        
        @function_tool()
        async def my_tool(x: str) -> str:
            """Test tool."""
            return x
        
        agent = Agent(client=AsyncMock(), tools=[my_tool])
        assert "my_tool" in agent.tools
    
    def test_register_sync_tool_raises(self):
        """Sync functions should not be registered directly."""
        def sync_tool():
            pass
        sync_tool.tool_spec = {"name": "sync_tool"}
        
        with pytest.raises(TypeError):
            Agent(client=AsyncMock(), tools=[sync_tool])
    
    def test_duplicate_tool_not_registered(self):
        from xagent.utils.tool_decorator import function_tool
        
        @function_tool(name="my_tool")
        async def tool_v1(x: str) -> str:
            return "v1"
        
        @function_tool(name="my_tool")  
        async def tool_v2(x: str) -> str:
            return "v2"
        
        agent = Agent(client=AsyncMock(), tools=[tool_v1, tool_v2])
        # First registered tool should be kept
        assert agent.tools["my_tool"] is tool_v1


class TestAgentSanitizeMessages:
    
    def test_removes_leading_function_call_output(self):
        from xagent.schemas.message import MessageType
        
        messages = [
            {"type": "function_call_output", "content": "result"},
            {"type": "message", "role": "user", "content": "hello"},
        ]
        
        result = Agent._sanitize_input_messages(messages.copy())
        assert len(result) == 1
        assert result[0]["type"] == "message"
    
    def test_does_not_modify_input_list(self):
        """Should not mutate the original list."""
        messages = [
            {"type": "function_call_output", "content": "result"},
        ]
        original_id = id(messages)
        
        result = Agent._sanitize_input_messages(messages.copy())
        # Original messages should be unchanged
        assert len(messages) == 1  # Original not modified
    
    def test_empty_list(self):
        assert Agent._sanitize_input_messages([]) == []
    
    def test_no_leading_function_call_output(self):
        messages = [{"type": "message", "role": "user", "content": "hello"}]
        result = Agent._sanitize_input_messages(messages.copy())
        assert result == messages
```

---

## 4. 集成测试

### 4.1 使用 pytest-httpserver Mock 外部服务

```python
# tests/integration/test_agent_with_tools.py
import pytest
import asyncio
from pytest_httpserver import HTTPServer
from xagent import Agent
from xagent.utils.tool_decorator import function_tool

@pytest.fixture
def http_tool_server(httpserver: HTTPServer):
    """Mock HTTP server for tool testing."""
    httpserver.expect_request("/calculate").respond_with_json({"result": 42})
    return httpserver

@pytest.mark.asyncio
async def test_agent_calls_tool_on_request(mock_openai_client, http_tool_server):
    """Test that agent correctly calls tools when needed."""
    
    @function_tool(name="calculate")
    async def calculate(expression: str) -> int:
        """Calculate a mathematical expression."""
        # Simplified: just return 42 for testing
        return 42
    
    # Configure mock to first return a tool call, then a final response
    tool_call_response = MagicMock()
    tool_call_response.output_text = None
    tool_call_response.output = [
        MagicMock(type="function_call", name="calculate", 
                  arguments='{"expression": "6 * 7"}', call_id="call_001")
    ]
    
    final_response = MagicMock()
    final_response.output_text = "The answer is 42"
    final_response.output = None
    
    mock_openai_client.responses.create.side_effect = [
        tool_call_response, 
        final_response
    ]
    
    agent = Agent(
        client=mock_openai_client,
        tools=[calculate]
    )
    
    result = await agent.chat(
        user_message="What is 6 times 7?",
        user_id="test_user"
    )
    
    assert result == "The answer is 42"
    # Verify the tool was called
    assert mock_openai_client.responses.create.call_count == 2
```

### 4.2 工作流集成测试

```python
# tests/integration/test_workflow.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from xagent.multi.workflow import SequentialWorkflow, WorkflowPatternType

@pytest.mark.asyncio
async def test_sequential_workflow_passes_context():
    """Test that sequential workflow passes each agent's output to the next."""
    
    call_log = []
    
    async def agent_chat_mock(name: str, user_message: str, **kwargs) -> str:
        call_log.append((name, user_message))
        return f"{name}_output"
    
    # Create mock agents
    agents = []
    for i in range(3):
        agent = MagicMock()
        agent.name = f"agent_{i}"
        
        captured_name = f"agent_{i}"
        agent.chat = AsyncMock(
            side_effect=lambda user_message, captured=captured_name, **kw: 
                agent_chat_mock(captured, user_message, **kw)
        )
        agents.append(agent)
    
    workflow = SequentialWorkflow(agents=agents)
    result = await workflow.execute(user_id="test", task="initial_task")
    
    # Verify chain: agent_0 gets initial_task, agent_1 gets agent_0_output, etc.
    assert call_log[0] == ("agent_0", "initial_task")
    assert call_log[1] == ("agent_1", "agent_0_output")
    assert call_log[2] == ("agent_2", "agent_1_output")
    
    assert result.pattern == WorkflowPatternType.SEQUENTIAL
    assert result.result == "agent_2_output"
```

---

## 5. 端到端测试

```python
# tests/e2e/test_http_server.py
# 需要真实的 OpenAI API Key，在 CI 中通过环境变量启用

import pytest
import httpx
import asyncio
from xagent import Agent
from xagent.interfaces.server import AgentHTTPServer

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_chat_endpoint():
    """Full end-to-end test of HTTP server chat endpoint."""
    
    agent = Agent(name="e2e_test_agent")
    server = AgentHTTPServer(agent=agent)
    
    # Start server in background
    import uvicorn
    config = uvicorn.Config(server.app, host="127.0.0.1", port=18999)
    uv_server = uvicorn.Server(config)
    
    server_task = asyncio.create_task(uv_server.serve())
    await asyncio.sleep(1)  # Wait for server to start
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "http://127.0.0.1:18999/chat",
                json={
                    "user_id": "test_user",
                    "session_id": "test_session",
                    "user_message": "Say exactly: 'E2E test passed'"
                },
                timeout=30.0
            )
        
        assert response.status_code == 200
        data = response.json()
        assert "reply" in data
        assert "E2E test passed" in str(data["reply"])
    
    finally:
        uv_server.should_exit = True
        await server_task
```

---

## 6. 测试工具与配置

### 6.1 pytest 配置

```toml
# pyproject.toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
markers = [
    "e2e: End-to-end tests requiring real API keys",
    "slow: Tests that take more than 10 seconds",
    "benchmark: Performance benchmark tests"
]
# 默认跳过 e2e 和 benchmark 测试
addopts = "-m 'not e2e and not benchmark'"
```

### 6.2 通用 Fixtures

```python
# tests/conftest.py
import pytest
from unittest.mock import AsyncMock, MagicMock

@pytest.fixture
def mock_openai_response(text: str = "Mock response"):
    """Create a mock OpenAI response."""
    response = MagicMock()
    response.output_text = text
    response.output = None
    response.output_parsed = None
    return response

@pytest.fixture  
def mock_openai_client(mock_openai_response):
    """Mock OpenAI async client."""
    client = AsyncMock()
    client.responses.create = AsyncMock(return_value=mock_openai_response)
    client.responses.parse = AsyncMock(return_value=mock_openai_response)
    return client

@pytest.fixture
def simple_agent(mock_openai_client):
    """Create a simple agent for testing."""
    from xagent import Agent
    return Agent(
        name="test_agent",
        model="gpt-4.1-mini",
        client=mock_openai_client
    )
```

### 6.3 CI/CD 集成

```yaml
# .github/workflows/tests.yml
name: Tests

on: [push, pull_request]

jobs:
  unit-tests:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.10", "3.11", "3.12"]
    
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      
      - name: Install dependencies
        run: pip install -e ".[dev]"
      
      - name: Run unit tests with coverage
        run: |
          pytest tests/unit/ \
            --cov=xagent \
            --cov-report=xml \
            --cov-report=term-missing \
            --cov-fail-under=60
      
      - name: Upload coverage
        uses: codecov/codecov-action@v4
        with:
          token: ${{ secrets.CODECOV_TOKEN }}  # Required for private repos; omit for public repos with tokenless upload
```

---

## 7. 测试策略优先级汇总

| 测试类型 | 覆盖目标 | 优先级 |
|----------|----------|--------|
| `tool_decorator.py` 单元测试 | 核心工具转换逻辑 | P1 |
| `workflow_dsl.py` 单元测试 | DSL 解析（无 API 调用） | P1 |
| `message.py` 单元测试 | 消息序列化/反序列化 | P1 |
| `agent.py` 单元测试（Mock） | Agent 核心逻辑 | P1 |
| 工作流集成测试 | 多 Agent 协作 | P2 |
| HTTP 服务器测试 | API 端点 | P2 |
| 记忆系统测试 | 记忆存取 | P2 |
| 端到端测试 | 完整用户流程 | P3 |
| 性能基准测试 | 性能回归检测 | P3 |
