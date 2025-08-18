"""
Graph Workflow Example - Demonstrates complex dependency patterns with parallel execution.

This example shows different graph workflow patterns:
1. A->B, A&B->C pattern (branch and converge)
2. Fan-out/Fan-in pattern (A -> [B,C,D] -> E)
3. Complex dependency graph with multiple parallel paths
"""

import asyncio
from xagent.core.agent import Agent
from xagent.multi.workflow import Workflow


async def example_branch_converge():
    """
    Example: A->B, A&B->C pattern
    A processes initial task -> B processes A's output -> C gets both A and B's outputs
    B and D can run in parallel after A completes.
    """
    print("=== Branch-Converge Pattern (A->B, A&B->C, A->D) ===")
    
    # Create agents
    agent_A = Agent(name="A", description="Initial analyzer")
    agent_B = Agent(name="B", description="Path 1 processor") 
    agent_C = Agent(name="C", description="Convergence synthesizer")
    agent_D = Agent(name="D", description="Path 2 processor")
    
    # Define dependencies: A->B, A&B->C, A->D
    dependencies = {
        "B": ["A"],      # B depends on A
        "C": ["A", "B"], # C depends on both A and B 
        "D": ["A"]       # D depends on A (can run parallel with B)
    }
    
    workflow = Workflow()
    
    result = await workflow.run_graph(
        agents=[agent_A, agent_B, agent_C, agent_D],
        dependencies=dependencies,
        task="Analyze the market trends for electric vehicles and provide comprehensive insights",
        max_concurrent=5
    )
    
    print(f"Final result: {result.result}")
    print(f"Execution time: {result.execution_time:.2f}s")
    print(f"Execution layers: {result.metadata['execution_layers']}")
    print(f"Total layers: {result.metadata['total_layers']}")
    print()


async def example_fan_out_fan_in():
    """
    Example: Fan-out/Fan-in pattern
    A -> [B, C, D] -> E
    After A completes, B, C, D run in parallel, then E gets all their outputs.
    """
    print("=== Fan-out/Fan-in Pattern (A -> [B,C,D] -> E) ===")
    
    # Create agents
    agent_A = Agent(name="A", description="Initial research agent")
    agent_B = Agent(name="B", description="Technical analysis expert")
    agent_C = Agent(name="C", description="Market analysis expert")
    agent_D = Agent(name="D", description="Risk analysis expert")
    agent_E = Agent(name="E", description="Final synthesizer")
    
    # Define dependencies: A -> [B,C,D] -> E
    dependencies = {
        "B": ["A"],
        "C": ["A"], 
        "D": ["A"],
        "E": ["B", "C", "D"]  # E gets all parallel results
    }
    
    workflow = Workflow()
    
    result = await workflow.run_graph(
        agents=[agent_A, agent_B, agent_C, agent_D, agent_E],
        dependencies=dependencies,
        task="Research and analyze the future of renewable energy technologies",
        max_concurrent=3
    )
    
    print(f"Final result: {result.result}")
    print(f"Execution time: {result.execution_time:.2f}s")
    print(f"Execution layers: {result.metadata['execution_layers']}")
    print()


async def example_complex_graph():
    """
    Example: Complex graph with multiple parallel paths
    A -> [B, C] -> D -> [E, F] -> G
    """
    print("=== Complex Graph Pattern ===")
    
    # Create agents
    agents = [
        Agent(name="A", description="Initial data collector"),
        Agent(name="B", description="Data processor 1"),
        Agent(name="C", description="Data processor 2"),
        Agent(name="D", description="Data integrator"),
        Agent(name="E", description="Analysis engine 1"),
        Agent(name="F", description="Analysis engine 2"),
        Agent(name="G", description="Final report generator")
    ]
    
    # Define complex dependencies
    dependencies = {
        "B": ["A"],
        "C": ["A"],
        "D": ["B", "C"],
        "E": ["D"],
        "F": ["D"],
        "G": ["E", "F"]
    }
    
    workflow = Workflow()
    
    result = await workflow.run_graph(
        agents=agents,
        dependencies=dependencies,
        task="Process customer feedback data and generate actionable insights",
        max_concurrent=4
    )
    
    print(f"Final result: {result.result}")
    print(f"Execution time: {result.execution_time:.2f}s")
    print(f"Execution layers: {result.metadata['execution_layers']}")
    print(f"Layer details:")
    for layer_info in result.metadata['layer_results']:
        print(f"  Layer {layer_info['layer']}: {layer_info['agents']}")
    print()


async def example_hybrid_with_graph():
    """
    Example: Hybrid workflow combining sequential, parallel, and graph patterns
    """
    print("=== Hybrid Workflow with Graph Stage ===")
    
    # Create agents for different stages
    researcher = Agent(name="researcher", description="Research specialist")
    planner = Agent(name="planner", description="Strategic planner")
    
    expert1 = Agent(name="expert1", description="Domain expert 1")
    expert2 = Agent(name="expert2", description="Domain expert 2") 
    expert3 = Agent(name="expert3", description="Domain expert 3")
    synthesizer = Agent(name="synthesizer", description="Knowledge synthesizer")
    
    reviewer = Agent(name="reviewer", description="Quality reviewer")
    
    # Define hybrid workflow stages
    stages = [
        {
            "pattern": "sequential",
            "agents": [researcher, planner],
            "task": "Research and create strategic plan for: {original_task}",
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
            "task": "Analyze the strategic plan and provide expert insights: {previous_result}",
            "name": "expert_analysis"
        },
        {
            "pattern": "sequential",
            "agents": [reviewer],
            "task": "Review and finalize the analysis: {previous_result}",
            "name": "final_review"
        }
    ]
    
    workflow = Workflow()
    
    result = await workflow.run_hybrid(
        task="Develop a comprehensive digital transformation strategy for manufacturing companies",
        stages=stages
    )
    
    print(f"Final result: {result['final_result']}")
    print(f"Total execution time: {result['total_execution_time']:.2f}s")
    print(f"Stages executed: {result['stages_executed']}")
    print(f"Stage patterns: {result['stage_patterns']}")
    print()


async def main():
    """Run all graph workflow examples."""
    print("Graph Workflow Examples")
    print("=" * 50)
    
    await example_branch_converge()
    await example_fan_out_fan_in()
    await example_complex_graph()
    await example_hybrid_with_graph()
    
    print("All examples completed!")


if __name__ == "__main__":
    asyncio.run(main())
