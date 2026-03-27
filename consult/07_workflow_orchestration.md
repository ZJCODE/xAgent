# 工作流编排改进

## 1. 当前工作流模式

xAgent 提供三种工作流模式：

| 模式 | 类 | 场景 |
|------|----|------|
| Sequential | `SequentialWorkflow` | A→B→C 流水线处理 |
| Parallel | `ParallelWorkflow` | 多 Agent 并行验证/综合 |
| Graph | `GraphWorkflow` | 复杂依赖图（含并行优化） |
| Hybrid | `Workflow.run_hybrid()` | 多阶段混合模式 |

---

## 2. 已发现的具体问题

### 2.1 SequentialWorkflow 中 image_source 不传递给后续步骤

**问题代码**：

```python
# workflow.py - SequentialWorkflow.execute()
for i, agent in enumerate(self.agents):
    if i == 0:
        result = await agent.chat(
            user_message=current_input, 
            user_id=user_id, 
            session_id=str(uuid.uuid4()),
            image_source=image_source  # ← 只有第一个 agent 收到图像
        )
    else:
        result = await agent.chat(
            user_message=current_input, 
            user_id=user_id, 
            session_id=str(uuid.uuid4())
            # ← 后续 agent 不传 image_source，即使它们需要
        )
```

这意味着在图像分析流水线中（如：图像描述 → 内容分析 → 报告生成），只有第一个 Agent 能看到图像。

**改进方案**：

```python
for i, agent in enumerate(self.agents):
    # 图像仅在明确需要时传递，或通过配置控制
    agent_image_source = image_source if i == 0 else None
    
    result = await agent.chat(
        user_message=current_input,
        user_id=user_id,
        session_id=str(uuid.uuid4()),
        image_source=agent_image_source
    )
```

实际上，当前代码的行为（只传给第一个）在大多数场景是合理的——后续 Agent 通过文本描述来理解图像内容。但应该允许用户控制：

```python
class SequentialWorkflow(BaseWorkflow):
    def __init__(
        self, 
        agents: List[Agent], 
        name: Optional[str] = None,
        pass_images_to_all: bool = False  # 新参数：是否将图像传递给所有步骤
    ):
        super().__init__(agents, name)
        self.pass_images_to_all = pass_images_to_all
    
    async def execute(self, user_id: str, task: str, image_source=None, ...) -> WorkflowResult:
        for i, agent in enumerate(self.agents):
            agent_images = image_source if (i == 0 or self.pass_images_to_all) else None
            result = await agent.chat(
                user_message=current_input,
                user_id=user_id,
                session_id=str(uuid.uuid4()),
                image_source=agent_images
            )
```

### 2.2 工作流没有错误恢复策略

**问题**：工作流在任何步骤失败时立即抛出异常，没有错误恢复机制：

```python
# SequentialWorkflow
except Exception as e:
    self.logger.error(f"Agent {agent.name} failed: {e}")
    raise RuntimeError(f"Sequential pipeline failed at agent {i+1}: {e}")

# GraphWorkflow
except Exception as e:
    self.logger.error(f"Agent {agent_name} failed: {e}")
    return agent_name, f"Error: {e}"  # 错误被转换为字符串，可能导致后续 Agent 基于错误输入工作
```

**改进方案**：提供可配置的错误处理策略：

```python
from enum import Enum

class ErrorStrategy(Enum):
    FAIL_FAST = "fail_fast"          # 任何错误立即失败（当前行为）
    CONTINUE_WITH_ERROR = "continue" # 记录错误，继续执行
    RETRY = "retry"                  # 重试失败的步骤
    SKIP = "skip"                    # 跳过失败的步骤

class BaseWorkflow(ABC):
    def __init__(
        self,
        agents: List[Agent],
        name: Optional[str] = None,
        error_strategy: ErrorStrategy = ErrorStrategy.FAIL_FAST,
        max_retries: int = 2,
    ):
        self.error_strategy = error_strategy
        self.max_retries = max_retries
    
    async def _execute_agent_with_strategy(
        self,
        agent: Agent,
        user_message: str,
        user_id: str,
        **kwargs
    ) -> Tuple[bool, str]:
        """Execute agent with configured error strategy.
        
        Returns:
            (success: bool, result: str)
        """
        for attempt in range(self.max_retries + 1):
            try:
                result = await agent.chat(
                    user_message=user_message,
                    user_id=user_id,
                    **kwargs
                )
                return True, str(result)
            
            except Exception as e:
                self.logger.error(
                    "Agent '%s' attempt %d/%d failed: %s",
                    agent.name, attempt + 1, self.max_retries + 1, e
                )
                
                if self.error_strategy == ErrorStrategy.FAIL_FAST:
                    raise
                
                if attempt < self.max_retries and self.error_strategy == ErrorStrategy.RETRY:
                    await asyncio.sleep(2 ** attempt)  # 指数退避
                    continue
                
                if self.error_strategy == ErrorStrategy.SKIP:
                    return False, ""
                
                # CONTINUE_WITH_ERROR
                return False, f"[Error in {agent.name}: {str(e)[:200]}]"
        
        return False, f"[{agent.name} failed after {self.max_retries + 1} attempts]"
```

### 2.3 GraphWorkflow 结果传递不携带上下文

**问题**：当一个 Agent 有多个依赖时，输入只是简单的字符串拼接，没有结构化上下文：

```python
def _prepare_agent_input(self, agent_name: str, original_task: str, results: Dict[str, str]) -> str:
    if len(deps) == 1:
        return results[deps[0]]  # 直接返回上一个 agent 的原始输出
    else:
        combined_input = f"Original task: {original_task}\n\n" + "\n\n".join(dep_results)
        return combined_input
```

**改进方案**：提供结构化的上下文传递：

```python
def _prepare_agent_input(
    self, 
    agent_name: str, 
    original_task: str, 
    results: Dict[str, Any],
    include_original_task: bool = True
) -> str:
    deps = self.dependencies.get(agent_name, [])
    
    if not deps:
        return original_task
    
    parts = []
    
    if include_original_task and len(deps) > 1:
        parts.append(f"## Original Task\n{original_task}")
    
    for dep in deps:
        dep_result = results.get(dep, "")
        # 清晰的结构化标题
        parts.append(f"## Output from {dep}\n{dep_result}")
    
    if len(deps) == 1 and not include_original_task:
        return results[deps[0]]
    
    # 添加明确的任务指令
    parts.append(f"## Your Task\nBased on the above context, complete: {agent_name}'s specific responsibility")
    
    return "\n\n".join(parts)
```

### 2.4 AutoWorkflow 依赖 LLM 决策质量

`Workflow` 类提供了 `run_auto()` 方法（基于 LLM 自动规划 Agent 数量和依赖关系），这是一个高风险功能：

- LLM 可能生成不合理的 Agent 规划（如创建 20 个 Agent 处理简单任务）
- 依赖 LLM 生成的 Agent 名称必须与提供的 Agent 名称完全匹配（脆弱）
- 成本不可预测

**改进建议**：为自动模式添加约束：

```python
class AutoWorkflowConfig(BaseModel):
    max_agents: int = Field(default=5, ge=1, le=10, description="Maximum agents to create")
    min_agents: int = Field(default=2, ge=1, description="Minimum agents to create")
    model: str = Field(default="gpt-4.1-mini", description="Model for planning")
    planning_budget_tokens: int = Field(default=2000, description="Max tokens for planning phase")

async def run_auto(
    self,
    task: str,
    config: Optional[AutoWorkflowConfig] = None,
    **kwargs
) -> WorkflowResult:
    config = config or AutoWorkflowConfig()
    
    # 限制规划阶段的资源使用
    planner = Agent(
        model=config.model,
        # ... 规划 agent 配置
    )
```

---

## 3. 缺失的工作流模式

### 3.1 Map-Reduce 模式

对于处理大量数据（文档集合、数据集），Map-Reduce 模式非常有价值：

```python
class MapReduceWorkflow(BaseWorkflow):
    """
    Map-Reduce pattern for processing large collections.
    
    Pattern:
    Input → [Map Agent × N chunks] → [Reduce Agent]
    
    Use cases:
    - Summarize multiple documents
    - Analyze large datasets
    - Process multiple files
    """
    
    def __init__(
        self,
        map_agent: Agent,           # 处理单个片段的 Agent
        reduce_agent: Agent,        # 汇总所有结果的 Agent
        chunk_size: int = 4000,     # 每个 chunk 的字符数
        max_parallel: int = 5,
        name: Optional[str] = None
    ):
        super().__init__([map_agent, reduce_agent], name)
        self.map_agent = map_agent
        self.reduce_agent = reduce_agent
        self.chunk_size = chunk_size
        self.max_parallel = max_parallel
    
    async def execute(
        self,
        user_id: str,
        task: str,
        items: List[str],   # 要处理的数据列表
        image_source=None,
        **kwargs
    ) -> WorkflowResult:
        start_time = time.time()
        
        # Map phase: process each item in parallel
        semaphore = asyncio.Semaphore(self.max_parallel)
        
        async def map_item(item: str, idx: int) -> Tuple[int, str]:
            async with semaphore:
                result = await self.map_agent.chat(
                    user_message=f"{task}\n\nProcess this item:\n{item}",
                    user_id=user_id,
                    session_id=str(uuid.uuid4())
                )
                return idx, str(result)
        
        map_results = await asyncio.gather(*[
            map_item(item, i) for i, item in enumerate(items)
        ])
        
        # Sort by index to maintain order
        map_results.sort(key=lambda x: x[0])
        ordered_results = [result for _, result in map_results]
        
        # Reduce phase: synthesize all map results
        reduce_input = f"{task}\n\nSynthesize these {len(ordered_results)} results:\n\n"
        reduce_input += "\n\n---\n\n".join([
            f"Item {i+1}: {result}" 
            for i, result in enumerate(ordered_results)
        ])
        
        final_result = await self.reduce_agent.chat(
            user_message=reduce_input,
            user_id=user_id,
            session_id=str(uuid.uuid4())
        )
        
        return WorkflowResult(
            result=final_result,
            execution_time=time.time() - start_time,
            pattern=WorkflowPatternType.GRAPH,
            metadata={
                "pattern": "map_reduce",
                "items_processed": len(items),
                "map_results": ordered_results
            }
        )
```

### 3.2 路由模式（Router Pattern）

根据输入特征动态选择最合适的 Agent：

```python
class RouterWorkflow:
    """
    Dynamic routing pattern: route tasks to specialized agents.
    
    Use cases:
    - Language routing (Chinese → Chinese Agent, English → English Agent)
    - Domain routing (Code → Code Agent, Math → Math Agent)
    - Complexity routing (Simple → Fast Agent, Complex → Powerful Agent)
    """
    
    def __init__(
        self,
        agents: Dict[str, Agent],  # name -> agent
        router_agent: Optional[Agent] = None,  # if None, use LLM-based routing
    ):
        self.agents = agents
        self.router_agent = router_agent or self._create_default_router()
    
    def _create_default_router(self) -> Agent:
        """Create a default LLM-based router."""
        agent_descriptions = "\n".join([
            f"- {name}: {agent.description or 'No description'}"
            for name, agent in self.agents.items()
        ])
        
        return Agent(
            name="router",
            system_prompt=f"""You are a task router. Based on the user's request, 
select the most appropriate agent from:

{agent_descriptions}

Respond with ONLY the agent name."""
        )
    
    async def route(
        self,
        task: str,
        user_id: str = "default_user",
        **kwargs
    ) -> Tuple[str, str]:
        """Route task to appropriate agent and return (agent_name, result)."""
        
        # Determine which agent to use
        routing_decision = await self.router_agent.chat(
            user_message=f"Route this task to the best agent: {task}",
            user_id=user_id,
            session_id=f"router_{uuid.uuid4().hex[:8]}"
        )
        
        agent_name = str(routing_decision).strip()
        
        if agent_name not in self.agents:
            # Fallback to first available agent
            agent_name = list(self.agents.keys())[0]
            self.logger.warning("Router chose unknown agent '%s', using '%s'", routing_decision, agent_name)
        
        selected_agent = self.agents[agent_name]
        result = await selected_agent.chat(
            user_message=task,
            user_id=user_id,
            **kwargs
        )
        
        return agent_name, str(result)
```

---

## 4. DSL 增强

### 4.1 当前 DSL 的限制

当前 DSL（`workflow_dsl.py`）只支持线性依赖，缺少：
- 条件分支（`if-else`）
- 循环（`while`）
- 最大并发数注解

### 4.2 增强的 DSL 语法建议

```
# 基础（当前支持）
A -> B -> C

# 并行分支
A -> B & C -> D

# 条件分支（新）
A -> [condition: B if result.score > 0.8 else C] -> D

# 循环（新）
A -> [loop: B while needs_refinement, max=3] -> C

# 并发注解（新）
A -> B[timeout=30, retry=2] -> C

# 命名节点（新）
fetch_data:A -> process:B -> report:C
```

实现完整的条件分支和循环需要较大工作量，但可以从简单的超时和重试注解开始：

```python
def parse_dependencies_dsl_v2(dsl_string: str) -> Tuple[Dict[str, List[str]], Dict[str, dict]]:
    """
    Parse DSL with annotations.
    
    Returns:
        (dependencies, node_configs)
        node_configs: {agent_name: {timeout: float, retry: int, ...}}
    """
    # 解析节点注解
    # "B[timeout=30, retry=2]" -> ("B", {"timeout": 30, "retry": 2})
    node_annotation_pattern = re.compile(r'(\w+)\[([^\]]+)\]')
    
    node_configs = {}
    
    # 预处理：提取并移除注解
    def extract_annotations(text: str) -> str:
        for match in node_annotation_pattern.finditer(text):
            node_name = match.group(1)
            annotations = match.group(2)
            
            config = {}
            for ann in annotations.split(','):
                key, _, value = ann.strip().partition('=')
                try:
                    config[key.strip()] = float(value.strip())
                except ValueError:
                    config[key.strip()] = value.strip()
            
            node_configs[node_name] = config
        
        return node_annotation_pattern.sub(r'\1', text)
    
    cleaned_dsl = extract_annotations(dsl_string)
    dependencies = parse_dependencies_dsl(cleaned_dsl)
    
    return dependencies, node_configs
```

---

## 5. 工作流监控与可视化

### 5.1 执行可视化

为工作流执行添加可视化支持，方便调试：

```python
class WorkflowVisualizer:
    """Generate workflow execution visualizations."""
    
    @staticmethod
    def to_mermaid(
        workflow: BaseWorkflow, 
        result: Optional[WorkflowResult] = None
    ) -> str:
        """Generate Mermaid diagram for workflow."""
        lines = ["graph LR"]
        
        if isinstance(workflow, SequentialWorkflow):
            agents = workflow.agents
            for i in range(len(agents) - 1):
                lines.append(f"    {agents[i].name} --> {agents[i+1].name}")
        
        elif isinstance(workflow, GraphWorkflow):
            for agent_name, deps in workflow.dependencies.items():
                for dep in deps:
                    lines.append(f"    {dep} --> {agent_name}")
        
        # Add execution status if result is available
        if result and "all_results" in result.metadata:
            for agent_name, agent_result in result.metadata["all_results"].items():
                if str(agent_result).startswith("Error:"):
                    lines.append(f"    style {agent_name} fill:#ff6b6b")
                else:
                    lines.append(f"    style {agent_name} fill:#51cf66")
        
        return "\n".join(lines)
    
    @staticmethod
    def print_execution_summary(result: WorkflowResult) -> None:
        """Print a human-readable execution summary."""
        print(f"\n{'='*50}")
        print(f"Workflow: {result.pattern.value}")
        print(f"Total time: {result.execution_time:.2f}s")
        print(f"{'='*50}")
        
        if "layer_results" in result.metadata:
            for layer in result.metadata["layer_results"]:
                print(f"\nLayer {layer['layer']}: {', '.join(layer['agents'])}")
                for agent, res in layer["results"].items():
                    status = "❌" if str(res).startswith("Error:") else "✅"
                    print(f"  {status} {agent}: {str(res)[:80]}...")
```

---

## 6. 工作流改进优先级汇总

| 改进项 | 收益 | 难度 | 优先级 |
|--------|------|------|--------|
| 错误恢复策略 | 提升稳定性 | 中 | P1 |
| image_source 传递控制 | 修复 Bug | 低 | P1 |
| Map-Reduce 模式 | 扩展应用场景 | 中 | P2 |
| 路由模式 | 扩展应用场景 | 中 | P2 |
| 结构化上下文传递 | 提升输出质量 | 低 | P2 |
| 执行可视化 | 改善调试体验 | 低 | P3 |
| DSL 注解（超时/重试） | 提升配置灵活性 | 中 | P3 |
| AutoWorkflow 约束 | 防止资源滥用 | 低 | P3 |
