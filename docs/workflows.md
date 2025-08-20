# Multi-Agent Workflows

xAgent provides powerful workflow orchestration patterns for coordinating multiple agents to solve complex tasks. Choose the right pattern based on your task requirements.

## Workflow Patterns

| Pattern | Use Case | Example |
|---------|----------|---------|
| **Auto** | Intelligent automatic workflow generation | AI determines optimal agents and dependencies |
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
    
    # 0. Auto: Let AI decide the optimal workflow!
    result = await workflow.run_auto(
        task="Research AI trends, analyze market data, and write a comprehensive business strategy report"
    )
    print("Auto workflow result:", result.result)
    print(f"AI created {result.metadata['agent_count']} specialized agents")
    print(f"Reasoning: {result.metadata['agent_selection_reasoning']}")
    
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

## Workflow Pattern Comparison

| Aspect | Auto ⭐ | Sequential | Parallel | Graph | Hybrid |
|--------|---------|------------|----------|-------|--------|
| **Setup Time** | 0 mins | 15-30 mins | 10-20 mins | 30-60 mins | 60+ mins |
| **Expertise Required** | None | Medium | Medium | High | High |
| **Optimization** | AI-powered | Manual | Manual | Manual | Manual |
| **Scalability** | Automatic | Fixed | Fixed | Fixed | Fixed |
| **Agent Design** | AI-generated | Manual | Manual | Manual | Manual |
| **Dependencies** | AI-optimized | N/A | N/A | Manual | Manual |
| **Best For** | Any complex task | Simple pipelines | Consensus | Complex deps | Multi-stage |
| **Result Quality** | Highest | Good | Good | Very Good | Very Good |

## Pattern Selection Guide

### Auto Workflows ⭐ **RECOMMENDED**

**Choose Auto when:**
- You want optimal results with minimal setup
- Task complexity is unknown or variable
- You need intelligent agent specialization
- You want automatic dependency optimization

**Key Benefits:**
- **Zero Configuration**: No need to design agents or dependencies
- **Intelligent Analysis**: AI analyzes task complexity to determine optimal agent count (2-6)
- **Specialized Agents**: Each agent gets a role perfectly suited for the task
- **Optimal Dependencies**: AI creates efficient execution patterns with maximum parallelization
- **Structured Output**: Uses Pydantic models for reliable agent and dependency generation

**Example Use Cases:**
- Any complex task where you want the best results
- Business strategy development
- Research and analysis projects
- Content creation workflows
- Technical architecture design

```python
# Auto workflow - the easiest and most powerful option
result = await workflow.run_auto(
    task="Develop a go-to-market strategy for a new SaaS product in the healthcare space"
)

# AI automatically:
# 1. Analyzes task complexity
# 2. Creates 4-5 specialized agents (e.g., Market Researcher, Product Strategist, etc.)
# 3. Designs optimal dependencies for parallel execution
# 4. Executes the workflow efficiently

print(f"Generated {result.metadata['agent_count']} agents:")
for agent in result.metadata['generated_agents']:
    print(f"- {agent['name']}: {agent['system_prompt'][:100]}...")

print(f"Dependencies: {result.metadata['generated_dependencies']}")
print(f"Result: {result.result}")
```

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

### Auto Workflow Examples ⭐

```python
async def auto_workflow_examples():
    workflow = Workflow()
    
    # Simple task - AI creates 2-3 agents
    result = await workflow.run_auto(
        task="Write a blog post about renewable energy benefits"
    )
    print(f"Simple task: {result.metadata['agent_count']} agents created")
    
    # Complex task - AI creates 4-6 specialized agents
    result = await workflow.run_auto(
        task="""Develop a comprehensive digital transformation strategy for a traditional 
        manufacturing company, including technology assessment, implementation roadmap, 
        ROI analysis, risk management, and change management plan"""
    )
    print(f"Complex task: {result.metadata['agent_count']} agents created")
    print(f"AI reasoning: {result.metadata['agent_selection_reasoning']}")
    
    # Technical task - AI creates specialized technical agents
    result = await workflow.run_auto(
        task="""Design a scalable microservices architecture for a real-time 
        financial trading platform with sub-millisecond latency requirements"""
    )
    print(f"Technical task: {result.metadata['agent_count']} agents created")
    
    return result.result

# Run auto workflow
asyncio.run(auto_workflow_examples())
```

### Auto Workflow Advanced Features

#### Task Complexity Analysis
```python
# Auto workflow intelligently scales agent count based on task complexity
simple_task = "Write a product description"  # → 2-3 agents
moderate_task = "Create marketing strategy with market analysis"  # → 3-4 agents  
complex_task = "Design enterprise digital transformation roadmap"  # → 4-6 agents

workflow = Workflow()
result = await workflow.run_auto(task=complex_task)
print(f"Complexity analysis: {result.metadata['agent_selection_reasoning']}")
```

#### Structured Agent Generation
Auto workflow uses Pydantic models for reliable agent creation:
```python
# Internally uses these structures for reliable generation:
class AgentDependency(BaseModel):
    agent_name: str
    depends_on: List[str]

class DependenciesSpec(BaseModel):
    agent_dependencies: List[AgentDependency]
    explanation: str
```

#### Metadata and Analytics
```python
result = await workflow.run_auto(task="Complex business analysis")

# Rich metadata available
print(f"Agents created: {result.metadata['agent_count']}")
print(f"Generation time: {result.metadata['agent_generation_time']:.2f}s")
print(f"Dependencies time: {result.metadata['dependencies_generation_time']:.2f}s")
print(f"Execution layers: {result.metadata['total_layers']}")
print(f"Dependencies: {result.metadata['generated_dependencies']}")
```

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
