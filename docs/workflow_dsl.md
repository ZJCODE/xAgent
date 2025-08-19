# Workflow DSL (Domain Specific Language) Support

xAgent now supports using intuitive DSL syntax to define workflow dependencies, making complex dependency relationships more concise and readable.

## Syntax Overview

DSL uses arrow symbols and the ampersand symbol `&` to represent dependencies:

- **ASCII Arrow `->`**: `A->B`

Basic syntax rules:
- `A->B`: B depends on A
- `A->B->C`: Chain dependencies, A->B, B->C
- `A->B, A->C`: Parallel branches, both B and C depend on A
- `A&B->C`: C depends on both A and B
- `->A`: A is a root node (no dependencies)

## Basic Syntax

### 1. Simple Dependencies
```python
# DSL syntax
dependencies = "A->B"

# Equivalent dictionary syntax
dependencies = {"B": ["A"]}
```

**Graph Structure:**
```
A ──→ B
```

### 2. Chain Dependencies
```python
# DSL syntax
dependencies = "A->B->C->D"

# Equivalent dictionary syntax
dependencies = {
    "B": ["A"],
    "C": ["B"],
    "D": ["C"]
}
```

**Graph Structure:**
```
A ──→ B ──→ C ──→ D
```

### 3. Parallel Branches
```python
# DSL syntax
dependencies = "A->B, A->C"

# Equivalent dictionary syntax
dependencies = {
    "B": ["A"],
    "C": ["A"]
}
```

**Graph Structure:**
```
     ┌──→ B
A ───┤
     └──→ C
```

### 4. Multiple Dependencies Merge
```python
# DSL syntax
dependencies = "A&B->C"

# Equivalent dictionary syntax
dependencies = {"C": ["A", "B"]}
```

**Graph Structure:**
```
A ───┐
     ├──→ C
B ───┘
```

### 5. Complex Workflows
```python
# DSL syntax
dependencies = "A->B, A->C, B&C->D, A->E, D&E->F"

# Equivalent dictionary syntax
dependencies = {
    "B": ["A"],
    "C": ["A"],
    "D": ["B", "C"],
    "E": ["A"],
    "F": ["D", "E"]
}
```

**Graph Structure:**
```
     ┌──→ B ───┐
A ───┤         ├──→ D ───┐
     ├──→ C ───┘         ├──→ F
     └──→ E ─────────────┘
```

## Usage Examples

### Basic Usage

```python
from xagent.core.agent import Agent
from xagent.multi.workflow import Workflow

# Create agents
researcher = Agent(name="researcher", system_prompt="Research agent")
analyzer = Agent(name="analyzer", system_prompt="Analysis agent")
planner = Agent(name="planner", system_prompt="Planning agent")
synthesizer = Agent(name="synthesizer", system_prompt="Synthesis agent")

# Define workflow using DSL
workflow = Workflow()

result = await workflow.run_graph(
    agents=[researcher, analyzer, planner, synthesizer],
    dependencies="researcher->analyzer, researcher->planner, analyzer&planner->synthesizer",
    task="Research AI impact on education and create implementation plan"
)
```

### Research Workflow Example

```python
# Research workflow: Data collection -> Analysis/Planning -> Report writing

dependencies = "collect_data->analyze_data, collect_data->create_plan, analyze_data&create_plan->write_report"

# This creates the following execution graph:
# collect_data (Layer 1)
# ├── analyze_data (Layer 2, parallel)
# └── create_plan (Layer 2, parallel)
# └── write_report (Layer 3, waits for previous two to complete)
```

### Software Development Workflow Example

```python
# Software development workflow

# Dependency definition
dependencies = "requirements->design, requirements->research, design&research->implementation, implementation->testing, testing->deployment"

# Execution graph:
# requirements (Layer 1)
# ├── design (Layer 2, parallel)
# └── research (Layer 2, parallel)
# └── implementation (Layer 3, waits for design & research)
# └── testing (Layer 4)
# └── deployment (Layer 5)
```

## DSL in Hybrid Workflows

DSL can also be used in hybrid workflows:

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
        "dependencies": "analyzer->synthesizer, analyzer->validator, synthesizer&validator->final",
        "task": "Analyze and validate: {previous_result}",
        "name": "analysis_phase"
    }
]

result = await workflow.run_hybrid(
    task="Create comprehensive AI strategy",
    stages=stages
)
```

## Syntax Validation

DSL includes built-in syntax validation:

```python
from xagent.multi.workflow import validate_dsl_syntax, parse_dependencies_dsl

# Validate syntax
is_valid, error_message = validate_dsl_syntax("A->B, A->C, B&C->D")
if not is_valid:
    print(f"Syntax error: {error_message}")

# Parse to dictionary
dependencies_dict = parse_dependencies_dsl("A->B, A->C, B&C->D")
print(dependencies_dict)
# Output: {'B': ['A'], 'C': ['A'], 'D': ['B', 'C']}
```

## Common Patterns

### 1. Fan-out Pattern
```python
# One input, multiple parallel outputs
"input->process1, input->process2, input->process3"
```

### 2. Fan-in Pattern
```python
# Multiple inputs, one output
"source1&source2&source3->combiner"
```

### 3. Pipeline Pattern
```python
# Strict sequential processing
"stage1->stage2->stage3->stage4"
```

### 4. Diamond Pattern
```python
# Branch then merge
"start->branch1, start->branch2, branch1&branch2->end"
```

### 5. Complex DAG Pattern
```python
# Complex Directed Acyclic Graph
"A->B, A->C, B->D, C->D, B&C->E, D&E->F"
```

## Error Handling

Common syntax errors and solutions:

### Error 1: Incomplete Arrow
```python
# ❌ Wrong
"A->"  # Missing target

# ✅ Correct
"A->B"
```

### Error 2: Empty Dependencies
```python
# ❌ Wrong
"A&->B"  # Empty dependency

# ✅ Correct
"A&C->B"
```

### Error 3: Double Arrow Syntax
```python
# ❌ Wrong
"A-->B"  # Using double arrow

# ✅ Correct
"A->B"
```

## Advantages

### 1. High Readability
```python
# DSL: Dependencies are immediately clear
"research->analysis, research->planning, analysis&planning->synthesis"

# Dictionary: Requires careful reading
{
    "analysis": ["research"],
    "planning": ["research"], 
    "synthesis": ["analysis", "planning"]
}
```

### 2. Conciseness
Complex dependency relationships are more concise when expressed with DSL, especially chain dependencies.

### 3. Intuitiveness
The use of arrows and ampersand symbols aligns with human thinking patterns, making it easier to understand and maintain.

### 4. Compatibility
DSL is fully compatible with existing dictionary formats, allowing seamless migration.

## Best Practices

1. **Use descriptive agent names**: Use meaningful names like `data_collector`, `analyzer` instead of `A`, `B`

2. **Group related rules appropriately**: Keep related dependencies together
   ```python
   # Good practice
   "collect->analyze, collect->plan, analyze&plan->report"
   
   # Not so good
   "collect->analyze, plan->report, collect->plan, analyze->report"
   ```

3. **Use appropriate spacing**: Improve readability
   ```python
   # Recommended
   "A -> B, A -> C, B & C -> D"
   
   # Also acceptable
   "A->B, A->C, B&C->D"
   ```

4. **Validate complex DSL**: For complex workflows, validate syntax first
   ```python
   dsl = "complex->workflow->with->many->dependencies"
   is_valid, error = validate_dsl_syntax(dsl)
   if not is_valid:
       print(f"Please check syntax: {error}")
   ```

This DSL extension makes xAgent's workflow definition more intuitive and user-friendly, particularly suitable for complex multi-agent coordination scenarios.
