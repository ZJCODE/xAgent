# Agent 能力增强

## 1. 当前 Agent 能力评估

| 能力维度 | 当前状态 | 成熟度 |
|----------|----------|--------|
| 多轮对话 | ✅ 完整 | 高 |
| 工具调用 | ✅ 完整 | 高 |
| 结构化输出 | ✅ 完整 | 高 |
| 流式响应 | ✅ 基础 | 中 |
| 多模态输入 | ✅ 图像 | 中 |
| MCP 集成 | ✅ 基础 | 中 |
| 长期记忆 | ✅ 基础 | 中 |
| Sub-agent 协作 | ✅ 工具化 | 中 |
| 群体智能 (Swarm) | ❌ 未实现 | 低 |
| 规划能力 | ❌ 缺失 | 低 |
| 反思能力 | ❌ 缺失 | 低 |
| 工具错误恢复 | ⚠️ 基础 | 低 |

---

## 2. 规划能力（Planning）

### 2.1 为什么规划能力重要

当前 Agent 是纯粹的"反应式"（Reactive）Agent：接收用户输入 → 调用工具 → 返回结果。对于复杂任务（如"帮我分析竞争对手并生成报告"），这种模式往往产出低质量结果，因为 Agent 没有明确的计划。

### 2.2 推荐方案：ReAct + Plan-and-Execute 混合

```python
class PlanningAgent(Agent):
    """
    Agent with explicit planning capabilities.
    
    Uses Plan-and-Execute pattern for complex multi-step tasks:
    1. Analyze task and create a structured plan
    2. Execute each step sequentially or in parallel
    3. Adapt plan based on intermediate results
    """
    
    async def chat(
        self, 
        user_message: str, 
        enable_planning: bool = False,
        **kwargs
    ) -> Union[str, BaseModel]:
        if not enable_planning:
            # Fall back to standard ReAct loop
            return await super().chat(user_message, **kwargs)
        
        # Phase 1: Create execution plan
        plan = await self._create_plan(user_message)
        self.logger.info("Execution plan created with %d steps", len(plan.steps))
        
        # Phase 2: Execute plan steps
        results = {}
        for step in plan.steps:
            self.logger.info("Executing step: %s", step.name)
            
            # Resolve dependencies from previous steps
            step_input = self._resolve_step_input(step, user_message, results)
            
            result = await super().chat(
                user_message=step_input,
                session_id=f"{kwargs.get('session_id', 'default')}_step_{step.id}",
                **{k: v for k, v in kwargs.items() if k != 'session_id'}
            )
            results[step.id] = result
        
        # Phase 3: Synthesize final answer
        return await self._synthesize_results(user_message, plan, results)
    
    async def _create_plan(self, task: str) -> ExecutionPlan:
        """Create a structured execution plan for the given task."""
        from pydantic import BaseModel, Field
        
        class PlanStep(BaseModel):
            id: str
            name: str
            description: str
            depends_on: List[str] = Field(default_factory=list)
            tools_to_use: List[str] = Field(default_factory=list)
        
        class ExecutionPlan(BaseModel):
            goal: str
            steps: List[PlanStep]
            reasoning: str
        
        planner = Agent(
            name="planner",
            system_prompt="""You are an expert task planner. Break complex tasks into clear,
            executable steps. Each step should be atomic and have clear inputs/outputs.
            Consider tool availability when planning.""",
            output_type=ExecutionPlan
        )
        
        available_tools = list(self.tools.keys()) + list(self.mcp_tools.keys())
        
        plan = await planner.chat(
            user_message=f"""Task: {task}
            
Available tools: {available_tools}

Create a clear execution plan to accomplish this task."""
        )
        
        return plan
```

---

## 3. 反思能力（Reflection）

### 3.1 为什么反思能力重要

顶级 AI Agent 系统（如 AlphaCode、GPT-o1）的核心优势之一是"自我批评"能力 —— Agent 会检查自己的输出，识别错误，并进行修正。

### 3.2 推荐实现：Reflexion 模式

```python
class ReflectiveAgent(Agent):
    """
    Agent with self-reflection and self-correction capabilities.
    
    Based on the Reflexion paper (Shinn et al., 2023):
    After each action, the agent evaluates its output and decides 
    whether to revise or proceed.
    """
    
    async def _call_model_with_reflection(
        self,
        input_msgs: list,
        user_id: str,
        session_id: str,
        output_type: Optional[type[BaseModel]] = None,
        max_reflections: int = 2,
        **kwargs
    ) -> Tuple[ReplyType, Any]:
        """Execute model call with optional self-reflection."""
        
        reply_type, response = await self._call_model(
            input_msgs, user_id, session_id, output_type, **kwargs
        )
        
        if reply_type == ReplyType.SIMPLE_REPLY and max_reflections > 0:
            # Reflect on the response quality
            reflection = await self._reflect_on_response(
                original_query=input_msgs[-1].get("content", ""),
                response=str(response)
            )
            
            if reflection.needs_revision:
                self.logger.info(
                    "Reflection triggered revision: %s", 
                    reflection.feedback[:100]
                )
                
                # Add reflection context and retry
                reflection_msg = {
                    "role": "user",
                    "content": f"""Your previous response had issues:

{reflection.feedback}

Please provide a revised, improved response."""
                }
                
                revised_msgs = input_msgs + [
                    {"role": "assistant", "content": str(response)},
                    reflection_msg
                ]
                
                return await self._call_model_with_reflection(
                    revised_msgs, user_id, session_id, output_type,
                    max_reflections=max_reflections - 1,
                    **kwargs
                )
        
        return reply_type, response
    
    async def _reflect_on_response(
        self,
        original_query: str,
        response: str
    ) -> ReflectionResult:
        """Evaluate response quality and identify issues."""
        
        class ReflectionResult(BaseModel):
            needs_revision: bool = Field(
                description="Whether the response needs revision"
            )
            feedback: str = Field(
                description="Specific feedback on what needs improvement"
            )
            confidence_score: float = Field(
                description="Confidence in response quality (0-1)",
                ge=0.0, le=1.0
            )
        
        reflector = Agent(
            name="reflector",
            system_prompt="""You are a critical evaluator of AI responses. 
            Evaluate if the response fully addresses the query, is accurate, 
            and is well-structured. Be concise but specific in your feedback.""",
            output_type=ReflectionResult,
            model="gpt-4.1-nano"  # 使用较小模型降低成本
        )
        
        return await reflector.chat(
            user_message=f"""Query: {original_query}

Response: {response}

Evaluate this response. Does it fully and accurately address the query?"""
        )
```

---

## 4. 工具错误恢复能力

### 4.1 当前问题

当工具调用失败时，Agent 只收到一个错误字符串，无法自动恢复：

```python
try:
    result = await func(**args)
except Exception as e:
    self.logger.error("Tool call error: %s", e)
    result = f"Tool error: {e}"  # Agent 收到这个错误，但不知道该怎么办
```

### 4.2 改进方案：结构化工具错误 + 自动重试策略

```python
class ToolErrorInfo(BaseModel):
    """Structured tool error information for Agent to process."""
    tool_name: str
    error_type: str  # "timeout", "invalid_args", "permission_denied", "network", "unknown"
    error_message: str
    retryable: bool
    suggested_alternatives: List[str] = Field(default_factory=list)

class IntelligentToolHandler:
    """Handles tool errors with recovery strategies."""
    
    async def execute_with_recovery(
        self,
        func: Callable,
        args: dict,
        tool_name: str,
        agent: Agent,
        max_retries: int = 2
    ) -> str:
        """Execute tool with intelligent error recovery."""
        
        for attempt in range(max_retries + 1):
            try:
                return await asyncio.wait_for(func(**args), timeout=30.0)
            
            except asyncio.TimeoutError:
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)  # 指数退避
                    continue
                return self._format_error(ToolErrorInfo(
                    tool_name=tool_name,
                    error_type="timeout",
                    error_message=f"Tool '{tool_name}' timed out after 30s",
                    retryable=False,
                    suggested_alternatives=["Try breaking the task into smaller steps"]
                ))
            
            except PermissionError as e:
                return self._format_error(ToolErrorInfo(
                    tool_name=tool_name,
                    error_type="permission_denied",
                    error_message=str(e),
                    retryable=False,
                    suggested_alternatives=["Check tool permissions", f"Try alternative tools"]
                ))
            
            except Exception as e:
                if attempt < max_retries:
                    await asyncio.sleep(1)
                    continue
                return self._format_error(ToolErrorInfo(
                    tool_name=tool_name,
                    error_type="unknown",
                    error_message=str(e),
                    retryable=False
                ))
    
    def _format_error(self, error: ToolErrorInfo) -> str:
        """Format error for Agent consumption."""
        parts = [f"Tool '{error.tool_name}' failed ({error.error_type}): {error.error_message}"]
        if error.suggested_alternatives:
            parts.append(f"Suggestions: {'; '.join(error.suggested_alternatives)}")
        return "\n".join(parts)
```

---

## 5. 多模态能力扩展

### 5.1 当前状态

目前仅支持图像输入，且通过上传到图床实现（依赖第三方服务）。

### 5.2 扩展建议

```python
class MultiModalAgent(Agent):
    """Agent with comprehensive multi-modal capabilities."""
    
    async def chat(
        self,
        user_message: str,
        images: Optional[List[Union[str, bytes, Path]]] = None,
        audio: Optional[Union[str, bytes]] = None,
        documents: Optional[List[Union[str, bytes, Path]]] = None,
        **kwargs
    ) -> Union[str, BaseModel]:
        """
        Enhanced multi-modal chat.
        
        Args:
            user_message: Text input
            images: Image URLs, file paths, or bytes
            audio: Audio file path or bytes (for transcription)
            documents: Document paths for extraction (PDF, DOCX, etc.)
        """
        # Process audio: transcribe to text
        if audio:
            transcript = await self._transcribe_audio(audio)
            user_message = f"{user_message}\n\n[Audio Transcript]: {transcript}"
        
        # Process documents: extract text
        if documents:
            doc_content = await self._extract_document_content(documents)
            user_message = f"{user_message}\n\n[Document Content]:\n{doc_content}"
        
        # Handle images
        image_sources = None
        if images:
            image_sources = await self._process_images(images)
        
        return await super().chat(
            user_message=user_message,
            image_source=image_sources,
            **kwargs
        )
    
    async def _transcribe_audio(self, audio: Union[str, bytes]) -> str:
        """Transcribe audio using OpenAI Whisper."""
        import openai
        
        if isinstance(audio, str):
            with open(audio, "rb") as f:
                audio_data = f.read()
        else:
            audio_data = audio
        
        response = await self.client.audio.transcriptions.create(
            model="whisper-1",
            file=("audio.mp3", audio_data, "audio/mpeg")
        )
        return response.text
```

---

## 6. Agent 状态持久化

### 6.1 当前问题

Agent 实例重启后，所有状态（工具注册、MCP 工具缓存）都丢失，每次都需要重新初始化。

### 6.2 改进方案

```python
from dataclasses import dataclass, asdict
import json

@dataclass
class AgentState:
    """Serializable agent state for persistence."""
    name: str
    model: str
    system_prompt: str
    tool_names: List[str]           # 已注册的工具名
    mcp_servers: List[str]          # MCP 服务器列表
    mcp_tools_last_updated: Optional[float]
    created_at: float
    last_active: float

class StatefulAgent(Agent):
    """Agent with state persistence capabilities."""
    
    async def save_state(self, storage_path: str) -> None:
        """Save agent state to storage."""
        state = AgentState(
            name=self.name,
            model=self.model,
            system_prompt=self.system_prompt,
            tool_names=list(self.tools.keys()),
            mcp_servers=self.mcp_servers,
            mcp_tools_last_updated=self.mcp_tools_last_updated,
            created_at=getattr(self, '_created_at', time.time()),
            last_active=time.time()
        )
        
        with open(storage_path, 'w') as f:
            json.dump(asdict(state), f, indent=2)
    
    @classmethod
    async def load_state(
        cls, 
        storage_path: str, 
        tools: List[Callable]
    ) -> 'StatefulAgent':
        """Restore agent from saved state."""
        with open(storage_path, 'r') as f:
            state_dict = json.load(f)
        
        agent = cls(
            name=state_dict['name'],
            model=state_dict['model'],
            system_prompt=state_dict['system_prompt'],
            tools=tools,
            mcp_servers=state_dict['mcp_servers']
        )
        
        # Restore MCP tools last updated timestamp
        if state_dict.get('mcp_tools_last_updated'):
            agent.mcp_tools_last_updated = state_dict['mcp_tools_last_updated']
        
        return agent
```

---

## 7. 内置工具库

### 7.1 当前状态

xAgent 没有提供任何内置工具，用户需要自己实现所有工具。对比竞争框架（LangChain 有数百个工具，AutoGPT 有文件系统工具），这是一个明显缺口。

### 7.2 建议内置工具

```python
# xagent/tools/builtin/__init__.py

from .web import web_search, fetch_url
from .code import python_executor, shell_command  
from .file import read_file, write_file, list_directory
from .data import json_query, csv_analyzer

__all__ = [
    "web_search",
    "fetch_url", 
    "python_executor",
    "shell_command",
    "read_file",
    "write_file",
    "list_directory",
    "json_query",
    "csv_analyzer",
]
```

```python
# xagent/tools/builtin/web.py

@function_tool(
    name="web_search",
    description="Search the web for current information",
    param_descriptions={
        "query": "The search query",
        "max_results": "Maximum number of results to return (1-10)"
    }
)
async def web_search(query: str, max_results: int = 5) -> List[dict]:
    """Search the web using DuckDuckGo (no API key required)."""
    try:
        from duckduckgo_search import AsyncDDGS
        async with AsyncDDGS() as ddgs:
            results = await ddgs.atext(query, max_results=max_results)
            return [{"title": r["title"], "url": r["href"], "snippet": r["body"]} 
                    for r in results]
    except ImportError:
        raise ImportError("Install duckduckgo-search: pip install duckduckgo-search")
```

---

## 8. 动态工具加载

### 8.1 建议功能

允许在运行时动态注册/注销工具，无需重启 Agent：

```python
class Agent:
    def register_tool(self, tool: Callable) -> None:
        """Dynamically register a new tool at runtime."""
        if not asyncio.iscoroutinefunction(tool):
            raise TypeError(f"Tool '{tool.tool_spec['name']}' must be async")
        
        tool_name = tool.tool_spec['name']
        self.tools[tool_name] = tool
        self._tool_specs_cache = None  # Invalidate cache
        self.logger.info("Dynamically registered tool: %s", tool_name)
    
    def unregister_tool(self, tool_name: str) -> bool:
        """Remove a tool by name. Returns True if found and removed."""
        if tool_name in self.tools:
            del self.tools[tool_name]
            self._tool_specs_cache = None
            self.logger.info("Unregistered tool: %s", tool_name)
            return True
        return False
    
    @property
    def available_tools(self) -> List[str]:
        """Return list of all currently registered tool names."""
        return list(self.tools.keys()) + list(self.mcp_tools.keys())
```

---

## 9. Agent 能力增强优先级汇总

| 功能 | 业务价值 | 技术复杂度 | 优先级 |
|------|----------|----------|--------|
| 实现 Swarm | 高 | 中 | P1 |
| 工具超时+错误恢复 | 高 | 低 | P1 |
| 内置工具库 | 高 | 中 | P2 |
| 规划能力 | 高 | 中 | P2 |
| 动态工具加载 | 中 | 低 | P2 |
| 反思能力 | 中 | 中 | P3 |
| 状态持久化 | 中 | 中 | P3 |
| 音频多模态 | 中 | 中 | P3 |
| 文档多模态 | 中 | 低 | P3 |
