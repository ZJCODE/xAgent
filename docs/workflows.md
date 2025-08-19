# Multi-Agent Workflows

xAgent provides powerful workflow orchestration patterns for coordinating multiple agents to solve complex tasks. Choose the right pattern based on your task requirements.

## Workflow Patterns

| Pattern | Use Case | Example |
|---------|----------|---------|
| **Sequential** | Pipeline processing, step-by-step refinement | Research -> Analysis -> Summary |
| **Parallel** | Consensus building, multi-perspective analysis | Multiple experts solving same problem |
| **Graph** | Complex dependencies, fan-out/fan-in patterns | A->B, A->C, B&C->D |
| **Hybrid** | Multi-stage workflows combining patterns | Research (Sequential) -> Expert Review (Parallel) -> Final Report (Sequential) |

## Quick Start

```python
import asyncio
from xagent import Agent
from xagent.multi.workflow import Workflow

async def workflow_example():
    # Create specialized agents
    researcher = Agent(name="Researcher", system_prompt="Research specialist")
    analyst = Agent(name="Analyst", system_prompt="Data analysis expert")
    writer = Agent(name="Writer", system_prompt="Content writing specialist")
    
    # Initialize workflow orchestrator
    workflow = Workflow()
    
    # 1. Sequential: A -> B -> C
    result = await workflow.run_sequential(
        agents=[researcher, analyst, writer],
        task="Research AI trends and write a summary report"
    )
    print("Sequential result:", result.result)
    
    # 2. Parallel: All agents work on same task
    result = await workflow.run_parallel(
        agents=[researcher, analyst, writer],
        task="What are the key benefits of renewable energy?"
    )
    print("Parallel consensus:", result.result)
    
    # 3. Graph: Complex dependencies with DSL support
    # Traditional dictionary format
    dependencies = {
        "Analyst": ["Researcher"],      # Analyst depends on Researcher
        "Writer": ["Researcher", "Analyst"]  # Writer depends on both
    }
    
    # NEW: DSL format (more intuitive!)
    dependencies_dsl = "Researcher->Analyst, Researcher&Analyst->Writer"
    
    result = await workflow.run_graph(
        agents=[researcher, analyst, writer],
        dependencies=dependencies_dsl,  # Use DSL format
        task="Create comprehensive market analysis"
    )
    print("Graph result:", result.result)

asyncio.run(workflow_example())
```

## Pattern Selection Guide

### Sequential Workflows

**Choose Sequential when:**
- Tasks require step-by-step processing
- Each step builds on the previous one
- You need progressive refinement

**Example Use Cases:**
- Content creation pipeline: Research -> Outline -> Draft -> Review -> Final
- Data processing: Collection -> Cleaning -> Analysis -> Reporting
- Software development: Requirements -> Design -> Implementation -> Testing

```python
# Sequential workflow example
result = await workflow.run_sequential(
    agents=[data_collector, data_cleaner, data_analyzer, report_generator],
    task="Analyze customer satisfaction survey data"
)
```

### Parallel Workflows

**Choose Parallel when:**
- You want multiple perspectives on the same problem
- Need consensus or validation
- Quality assurance through redundancy

**Example Use Cases:**
- Expert panel reviews
- Multi-perspective analysis
- Consensus building
- Quality validation

```python
# Parallel workflow example
result = await workflow.run_parallel(
    agents=[expert1, expert2, expert3],
    task="Evaluate the feasibility of this business proposal"
)
```

### Graph Workflows

**Choose Graph when:**
- Tasks have complex dependencies
- Need parallel execution where possible
- Fan-out/fan-in patterns required

**Example Use Cases:**
- Research projects with multiple data sources
- Complex decision-making processes
- Multi-stage analysis with dependencies

```python
# Graph workflow with DSL
dependencies = "DataCollector->Analyzer1, DataCollector->Analyzer2, Analyzer1&Analyzer2->Synthesizer"

result = await workflow.run_graph(
    agents=[data_collector, analyzer1, analyzer2, synthesizer],
    dependencies=dependencies,
    task="Comprehensive market research analysis"
)
```

### Hybrid Workflows

**Choose Hybrid when:**
- Multi-stage complex workflows
- Different stages need different patterns
- Maximum flexibility and control

```python
async def hybrid_workflow_example():
    # Create workflow stages
    stages = [
        {
            "pattern": "sequential",
            "agents": [researcher, planner],
            "task": "Research and plan: {original_task}",
            "name": "research_phase"
        },
        {
            "pattern": "parallel", 
            "agents": [expert1, expert2, expert3],
            "task": "Review this research: {previous_result}",
            "name": "expert_review"
        },
        {
            "pattern": "sequential",
            "agents": [synthesizer],
            "task": "Create final report from: {previous_result}",
            "name": "final_synthesis"
        }
    ]
    
    workflow = Workflow()
    result = await workflow.run_hybrid(
        task="Analyze the future of electric vehicles",
        stages=stages
    )
    
    print("Final result:", result["final_result"])
    print("Execution time:", result["total_execution_time"])

asyncio.run(hybrid_workflow_example())
```

## Real-World Examples

### Content Creation Workflow

```python
async def content_creation_workflow():
    # Define agents
    researcher = Agent(
        name="ContentResearcher",
        system_prompt="Research topics and gather information",
        tools=[web_search]
    )
    
    writer = Agent(
        name="ContentWriter", 
        system_prompt="Create engaging content from research",
    )
    
    editor = Agent(
        name="ContentEditor",
        system_prompt="Edit and improve written content"
    )
    
    # Sequential workflow for content creation
    workflow = Workflow()
    result = await workflow.run_sequential(
        agents=[researcher, writer, editor],
        task="Create a blog post about sustainable technology"
    )
    
    return result.result
```

### Business Analysis Workflow

```python
async def business_analysis_workflow():
    # Market analysis agents
    market_analyst = Agent(name="MarketAnalyst", system_prompt="Analyze market trends")
    financial_analyst = Agent(name="FinancialAnalyst", system_prompt="Analyze financial data")
    risk_analyst = Agent(name="RiskAnalyst", system_prompt="Assess business risks")
    
    # Strategy synthesis agent
    strategist = Agent(name="Strategist", system_prompt="Synthesize analysis into strategy")
    
    # Graph workflow with complex dependencies
    dependencies = """
    MarketAnalyst->Strategist,
    FinancialAnalyst->Strategist,
    RiskAnalyst->Strategist
    """
    
    workflow = Workflow()
    result = await workflow.run_graph(
        agents=[market_analyst, financial_analyst, risk_analyst, strategist],
        dependencies=dependencies,
        task="Analyze market entry strategy for new product"
    )
    
    return result.result
```

### Research and Development Workflow

```python
async def research_development_workflow():
    # Research phase agents
    literature_reviewer = Agent(name="LiteratureReviewer", system_prompt="Review academic literature")
    data_collector = Agent(name="DataCollector", system_prompt="Collect and organize data")
    
    # Analysis phase agents  
    statistical_analyst = Agent(name="StatisticalAnalyst", system_prompt="Perform statistical analysis")
    qualitative_analyst = Agent(name="QualitativeAnalyst", system_prompt="Perform qualitative analysis")
    
    # Synthesis agent
    research_synthesizer = Agent(name="ResearchSynthesizer", system_prompt="Synthesize research findings")
    
    # Multi-stage hybrid workflow
    stages = [
        {
            "pattern": "parallel",
            "agents": [literature_reviewer, data_collector],
            "task": "Research background for: {original_task}",
            "name": "research_phase"
        },
        {
            "pattern": "parallel",
            "agents": [statistical_analyst, qualitative_analyst], 
            "task": "Analyze the data: {previous_result}",
            "name": "analysis_phase"
        },
        {
            "pattern": "sequential",
            "agents": [research_synthesizer],
            "task": "Synthesize findings: {previous_result}",
            "name": "synthesis_phase"
        }
    ]
    
    workflow = Workflow()
    result = await workflow.run_hybrid(
        task="Study the impact of remote work on team productivity",
        stages=stages
    )
    
    return result["final_result"]
```

## Workflow Configuration Best Practices

### Agent Specialization

- Design agents with clear, specific roles
- Use appropriate system prompts for specialization
- Provide relevant tools for each agent's domain

### Task Decomposition

- Break complex tasks into manageable subtasks
- Define clear handoff points between agents
- Ensure task descriptions are specific and actionable

### Dependency Management

- Use DSL notation for clear dependency visualization
- Avoid circular dependencies
- Plan for error handling and fallback scenarios

### Performance Optimization

- Consider parallel execution opportunities
- Balance agent workload distribution
- Monitor execution times and optimize bottlenecks

## Error Handling in Workflows

```python
async def robust_workflow_example():
    try:
        workflow = Workflow()
        result = await workflow.run_sequential(
            agents=[agent1, agent2, agent3],
            task="Process this complex task",
            max_retries=3,
            timeout=300  # 5 minutes timeout
        )
        return result.result
    except WorkflowTimeoutError:
        print("Workflow timed out")
    except AgentExecutionError as e:
        print(f"Agent {e.agent_name} failed: {e.error}")
    except Exception as e:
        print(f"Unexpected workflow error: {e}")
```

## Monitoring and Observability

### Workflow Metrics

- Execution time per agent and overall workflow
- Success/failure rates
- Task complexity metrics
- Resource utilization

### Logging and Debugging

```python
import logging

# Enable workflow debugging
logging.basicConfig(level=logging.DEBUG)

workflow = Workflow(debug=True)
result = await workflow.run_graph(
    agents=agents,
    dependencies=dependencies,
    task=task,
    log_intermediate_results=True
)
```

For detailed DSL syntax and examples, see [workflow_dsl.md](workflow_dsl.md).
