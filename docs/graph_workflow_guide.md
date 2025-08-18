# GraphWorkflow 使用指南

GraphWorkflow 是 xAgent 中新增的工作流模式，支持复杂的依赖关系和自动并行优化执行。

## 主要特性

1. **自动并行检测**: 自动识别可以并行执行的智能体节点
2. **拓扑排序**: 基于依赖关系自动确定执行顺序
3. **层级执行**: 将图分解为执行层，每层内并行执行
4. **依赖管理**: 正确处理复杂的依赖关系和数据传递
5. **并发控制**: 使用信号量控制最大并发数
6. **错误处理**: 单个节点失败不会影响其他并行节点

## 基本用法

### 1. 简单的分支-汇聚模式 (A->B, A&B->C)

```python
from xagent.core.agent import Agent
from xagent.multi.workflow import Workflow

# 创建智能体
agent_A = Agent(name="A", description="初始分析器")
agent_B = Agent(name="B", description="分支处理器")
agent_C = Agent(name="C", description="汇聚合成器")

# 定义依赖关系
dependencies = {
    "B": ["A"],      # B 依赖 A
    "C": ["A", "B"]  # C 依赖 A 和 B
}

# 执行图工作流
workflow = Workflow()
result = await workflow.run_graph(
    agents=[agent_A, agent_B, agent_C],
    dependencies=dependencies,
    task="分析市场趋势并提供建议"
)
```

### 2. 扇出-扇入模式 (A -> [B,C,D] -> E)

```python
# 创建智能体
agent_A = Agent(name="A", description="研究员")
agent_B = Agent(name="B", description="技术专家")
agent_C = Agent(name="C", description="市场专家")
agent_D = Agent(name="D", description="风险专家")
agent_E = Agent(name="E", description="综合分析师")

# 定义扇出-扇入依赖
dependencies = {
    "B": ["A"],
    "C": ["A"],
    "D": ["A"],
    "E": ["B", "C", "D"]  # E 汇聚所有并行结果
}

result = await workflow.run_graph(
    agents=[agent_A, agent_B, agent_C, agent_D, agent_E],
    dependencies=dependencies,
    task="研究新兴技术的发展前景"
)
```

### 3. 复杂图模式

```python
# A -> [B, C] -> D -> [E, F] -> G
dependencies = {
    "B": ["A"],
    "C": ["A"],      # B 和 C 可以并行
    "D": ["B", "C"], # D 汇聚 B 和 C
    "E": ["D"],
    "F": ["D"],      # E 和 F 可以并行
    "G": ["E", "F"]  # G 汇聚 E 和 F
}
```

## 混合工作流中使用 Graph 模式

GraphWorkflow 可以与 Sequential 和 Parallel 模式结合使用：

```python
stages = [
    {
        "pattern": "sequential",
        "agents": [researcher, planner],
        "task": "研究并制定策略: {original_task}",
        "name": "research_planning"
    },
    {
        "pattern": "graph",
        "agents": [expert1, expert2, expert3, synthesizer],
        "dependencies": {
            "expert2": ["expert1"],
            "expert3": ["expert1"],
            "synthesizer": ["expert2", "expert3"]
        },
        "task": "专家分析策略: {previous_result}",
        "name": "expert_analysis"
    },
    {
        "pattern": "sequential",
        "agents": [reviewer],
        "task": "审核最终结果: {previous_result}",
        "name": "final_review"
    }
]

result = await workflow.run_hybrid(
    task="制定数字化转型战略",
    stages=stages
)
```

## 依赖关系说明

依赖关系字典 `dependencies` 的格式为：
- **键**: 智能体名称
- **值**: 该智能体依赖的其他智能体列表

```python
dependencies = {
    "B": ["A"],           # B 等待 A 完成
    "C": ["A", "B"],      # C 等待 A 和 B 都完成
    "D": ["A"]            # D 只等待 A（可以与 B 并行）
}
```

## 数据传递规则

1. **无依赖**: 智能体接收原始任务
2. **单一依赖**: 智能体接收依赖智能体的输出
3. **多个依赖**: 智能体接收原始任务和所有依赖智能体的输出

```python
# 多依赖输入格式示例
input_text = f"""
Original task: {original_task}

Result from A:
{result_from_A}

Result from B:
{result_from_B}
"""
```

## 执行层次

GraphWorkflow 自动将图分解为执行层：

```
Layer 1: [A]           # 无依赖的节点
Layer 2: [B, D]        # 依赖 Layer 1，可并行执行
Layer 3: [C]           # 依赖 Layer 2
```

## 参数说明

### run_graph 方法参数

- `agents`: 智能体列表
- `dependencies`: 依赖关系字典
- `task`: 原始任务字符串
- `image_source`: 可选的图像源（仅传递给根节点）
- `max_concurrent`: 最大并发执行数（默认 10）
- `user_id`: 用户标识符

### 返回结果

```python
class WorkflowResult:
    result: Any                    # 最终结果
    execution_time: float          # 执行时间
    pattern: WorkflowPatternType   # 模式类型 (GRAPH)
    metadata: Dict                 # 元数据，包含：
        # - execution_layers: 执行层次
        # - layer_results: 每层的详细结果
        # - dependencies: 依赖关系
        # - all_results: 所有智能体的结果
        # - final_agents: 最终输出智能体
        # - total_agents: 智能体总数
        # - total_layers: 层数
```

## 最佳实践

1. **合理设计依赖**: 尽量减少不必要的依赖以提高并行度
2. **控制并发数**: 根据系统资源调整 `max_concurrent` 参数
3. **智能体命名**: 使用清晰的名称以便于调试和理解
4. **错误处理**: 考虑关键路径上的智能体失败场景
5. **结果验证**: 在复杂图中验证最终结果的完整性

## 常见模式

### 1. 分支处理
```python
# A -> [B, C, D] (一对多分发)
dependencies = {"B": ["A"], "C": ["A"], "D": ["A"]}
```

### 2. 汇聚处理
```python
# [A, B, C] -> D (多对一汇聚)
dependencies = {"D": ["A", "B", "C"]}
```

### 3. 管道并行
```python
# A -> B -> C 同时 A -> D -> E
dependencies = {"B": ["A"], "C": ["B"], "D": ["A"], "E": ["D"]}
```

### 4. 分层处理
```python
# 第一层: A
# 第二层: B, C (依赖 A)
# 第三层: D (依赖 B, C)
dependencies = {"B": ["A"], "C": ["A"], "D": ["B", "C"]}
```

GraphWorkflow 为复杂的多智能体协作提供了强大而灵活的解决方案，能够自动优化执行效率并正确处理依赖关系。
