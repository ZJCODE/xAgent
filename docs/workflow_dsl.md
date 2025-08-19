# Workflow DSL (Domain Specific Language) Support

xAgent 现在支持使用直观的 DSL 语法来定义工作流依赖关系，让复杂的依赖关系变得更加简洁和易读。

## 语法概览

DSL 使用箭头符号和与符号 `&` 来表示依赖关系，支持两种箭头格式：

- **Unicode 箭头 `→`**: `A→B` (推荐用于文档和展示)
- **ASCII 箭头 `->`**: `A->B` (推荐用于代码，更好的兼容性)

基本语法规则：
- `A→B` 或 `A->B`: B 依赖于 A
- `A→B→C` 或 `A->B->C`: 链式依赖，A→B，B→C
- `A→B, A→C` 或 `A->B, A->C`: 并行分支，B和C都依赖于A
- `A&B→C` 或 `A&B->C`: C 依赖于 A 和 B
- `→A` 或 `->A`: A 是根节点（无依赖）
- **混合使用**: `A→B, B->C, C→D` (可以在同一个 DSL 字符串中混合使用两种箭头)

## 基本语法

### 1. 简单依赖
```python
# DSL 语法 (两种箭头等效)
dependencies = "A→B"      # Unicode 箭头
dependencies = "A->B"     # ASCII 箭头

# 等效的字典语法
dependencies = {"B": ["A"]}
```

### 2. 链式依赖
```python
# DSL 语法
dependencies = "A→B→C→D"     # Unicode
dependencies = "A->B->C->D"  # ASCII

# 等效的字典语法
dependencies = {
    "B": ["A"],
    "C": ["B"],
    "D": ["C"]
}
```

### 3. 并行分支
```python
# DSL 语法
dependencies = "A→B, A→C"     # Unicode
dependencies = "A->B, A->C"   # ASCII

# 等效的字典语法
dependencies = {
    "B": ["A"],
    "C": ["A"]
}
```

### 4. 多依赖合并
```python
# DSL 语法
dependencies = "A&B→C"      # Unicode
dependencies = "A&B->C"     # ASCII

# 等效的字典语法
dependencies = {"C": ["A", "B"]}
```

### 5. 复杂工作流
```python
# DSL 语法
dependencies = "A→B, A→C, B&C→D, A→E, D&E→F"      # Unicode
dependencies = "A->B, A->C, B&C->D, A->E, D&E->F"  # ASCII

# 等效的字典语法
dependencies = {
    "B": ["A"],
    "C": ["A"],
    "D": ["B", "C"],
    "E": ["A"],
    "F": ["D", "E"]
}
```

### 6. 混合箭头使用
```python
# 可以在同一个 DSL 字符串中混合使用两种箭头
dependencies = "A→B, B->C, C→D->E"
# 解析结果: {"B": ["A"], "C": ["B"], "D": ["C"], "E": ["D"]}
```

## 使用示例

### 基本用法

```python
from xagent.core.agent import Agent
from xagent.multi.workflow import Workflow

# 创建 agents
researcher = Agent(name="researcher", system_prompt="Research agent")
analyzer = Agent(name="analyzer", system_prompt="Analysis agent")
planner = Agent(name="planner", system_prompt="Planning agent")
synthesizer = Agent(name="synthesizer", system_prompt="Synthesis agent")

# 使用 DSL 定义工作流 (可以选择任一箭头格式)
workflow = Workflow()

# Unicode 箭头版本
result = await workflow.run_graph(
    agents=[researcher, analyzer, planner, synthesizer],
    dependencies="researcher→analyzer, researcher→planner, analyzer&planner→synthesizer",
    task="Research AI impact on education and create implementation plan"
)

# ASCII 箭头版本 (等效)
result = await workflow.run_graph(
    agents=[researcher, analyzer, planner, synthesizer],
    dependencies="researcher->analyzer, researcher->planner, analyzer&planner->synthesizer",
    task="Research AI impact on education and create implementation plan"
)

# 混合使用版本
result = await workflow.run_graph(
    agents=[researcher, analyzer, planner, synthesizer],
    dependencies="researcher→analyzer, researcher->planner, analyzer&planner->synthesizer",
    task="Research AI impact on education and create implementation plan"
)
```

### 研究工作流示例

```python
# 研究工作流：数据收集 → 分析/计划 → 报告撰写

# Unicode 版本
dependencies = "collect_data→analyze_data, collect_data→create_plan, analyze_data&create_plan→write_report"

# ASCII 版本 (等效)
dependencies = "collect_data->analyze_data, collect_data->create_plan, analyze_data&create_plan->write_report"

# 这创建了以下执行图：
# collect_data (第1层)
# ├── analyze_data (第2层，并行)
# └── create_plan (第2层，并行)
# └── write_report (第3层，等待前两个完成)
```

### 软件开发工作流示例

```python
# 软件开发工作流

# Unicode 版本
dependencies = "requirements→design, requirements→research, design&research→implementation, implementation→testing, testing→deployment"

# ASCII 版本 (推荐用于代码)
dependencies = "requirements->design, requirements->research, design&research->implementation, implementation->testing, testing->deployment"

# 执行图：
# requirements (第1层)
# ├── design (第2层，并行)
# └── research (第2层，并行)
# └── implementation (第3层，等待 design & research)
# └── testing (第4层)
# └── deployment (第5层)
```

## 混合工作流中的 DSL

DSL 也可以在混合工作流中使用：

```python
stages = [
    {
        "pattern": "sequential",
        "agents": [researcher, planner],
        "task": "Research and plan: {original_task}",
        "name": "research_phase"
    },
    {
        "pattern": "graph",
        "agents": [analyzer, synthesizer, validator],
        "dependencies": "analyzer→synthesizer, analyzer→validator, synthesizer&validator→final",
        "task": "Analyze and validate: {previous_result}",
        "name": "analysis_phase"
    }
]

result = await workflow.run_hybrid(
    task="Create comprehensive AI strategy",
    stages=stages
)
```

## 语法验证

DSL 包含内置的语法验证：

```python
from xagent.multi.workflow import validate_dsl_syntax, parse_dependencies_dsl

# 验证语法
is_valid, error_message = validate_dsl_syntax("A→B, A→C, B&C→D")
if not is_valid:
    print(f"语法错误: {error_message}")

# 解析为字典
dependencies_dict = parse_dependencies_dsl("A→B, A→C, B&C→D")
print(dependencies_dict)
# 输出: {'B': ['A'], 'C': ['A'], 'D': ['B', 'C']}
```

## 常见模式

### 1. Fan-out 模式（扇出）
```python
# 一个输入，多个并行输出
"input→process1, input→process2, input→process3"
```

### 2. Fan-in 模式（扇入）
```python
# 多个输入，一个输出
"source1&source2&source3→combiner"
```

### 3. Pipeline 模式（管道）
```python
# 严格的序列处理
"stage1→stage2→stage3→stage4"
```

### 4. Diamond 模式（菱形）
```python
# 分支后合并
"start→branch1, start→branch2, branch1&branch2→end"
```

### 5. Complex DAG 模式
```python
# 复杂有向无环图
"A→B, A→C, B→D, C→D, B&C→E, D&E→F"
```

## 错误处理

常见的语法错误和解决方案：

### 错误1：箭头不完整
```python
# ❌ 错误
"A→"  # 缺少目标

# ✅ 正确
"A→B"
```

### 错误2：空依赖
```python
# ❌ 错误
"A&→B"  # 空依赖

# ✅ 正确
"A&C→B"
```

### 错误3：无效字符
```python
# ❌ 错误
"A->B"  # 使用了 -> 而不是 →

# ✅ 正确
"A→B"
```

## 优势

### 1. 可读性强
```python
# DSL: 一眼就能看懂依赖关系
"research→analysis, research→planning, analysis&planning→synthesis"

# Dictionary: 需要仔细阅读
{
    "analysis": ["research"],
    "planning": ["research"], 
    "synthesis": ["analysis", "planning"]
}
```

### 2. 简洁性
复杂的依赖关系用 DSL 表示更加简洁，特别是链式依赖。

### 3. 直观性
箭头和与符号的使用符合人类的思维习惯，更容易理解和维护。

### 4. 兼容性
DSL 与现有的字典格式完全兼容，可以无缝迁移。

## 最佳实践

1. **使用描述性的 agent 名称**：使用有意义的名称如 `data_collector`, `analyzer` 而不是 `A`, `B`

2. **合理分组规则**：将相关的依赖关系放在一起
   ```python
   # 好的做法
   "collect→analyze, collect→plan, analyze&plan→report"
   
   # 不太好的做法  
   "collect→analyze, plan→report, collect→plan, analyze→report"
   ```

3. **适当使用空格**：增加可读性
   ```python
   # 推荐
   "A → B, A → C, B & C → D"
   
   # 也可以
   "A→B, A→C, B&C→D"
   ```

4. **验证复杂的 DSL**：对于复杂的工作流，先验证语法
   ```python
   dsl = "complex→workflow→with→many→dependencies"
   is_valid, error = validate_dsl_syntax(dsl)
   if not is_valid:
       print(f"请检查语法: {error}")
   ```

这个 DSL 扩展使得 xAgent 的工作流定义更加直观和用户友好，特别适合复杂的多 agent 协调场景。
