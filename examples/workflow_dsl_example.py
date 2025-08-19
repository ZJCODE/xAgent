#!/usr/bin/env python3
"""
Example demonstrating the DSL (Domain Specific Language) support for workflow dependencies.

This example shows how to use arrow notation (→) and ampersand (&) to define
complex workflow dependencies in a more intuitive way.
"""

import asyncio
import sys
import os

# Add the parent directory to the path so we can import xagent
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xagent.core.agent import Agent
from xagent.multi.workflow import Workflow, parse_dependencies_dsl, validate_dsl_syntax


async def demonstrate_dsl_parsing():
    """Demonstrate DSL parsing capabilities."""
    print("=== DSL Parsing Examples ===\n")
    
    # Example DSL strings
    examples = [
        "A→B",                    # Simple: A depends on nothing, B depends on A
        "A→B→C",                  # Chain: A→B→C 
        "A→B, A→C",               # Parallel: A→B and A→C (B and C can run in parallel)
        "A→B, A→C, B&C→D",        # Complex: A→B, A→C, then D depends on both B and C
        "research→analysis, research→planning, analysis&planning→synthesis",  # Real workflow
    ]
    
    for dsl in examples:
        print(f"DSL: '{dsl}'")
        
        # Validate syntax
        is_valid, error = validate_dsl_syntax(dsl)
        if not is_valid:
            print(f"  ❌ Syntax Error: {error}\n")
            continue
        
        # Parse to dependencies
        deps = parse_dependencies_dsl(dsl)
        print(f"  ✅ Parsed Dependencies: {deps}")
        
        # Show execution layers
        print(f"  📊 This creates the following dependency graph:")
        if not deps:
            print(f"     - All agents run independently")
        else:
            for agent, agent_deps in deps.items():
                if agent_deps:
                    print(f"     - {agent} depends on: {', '.join(agent_deps)}")
                else:
                    print(f"     - {agent} has no dependencies")
        print()


async def run_workflow_with_dsl():
    """Run an actual workflow using DSL syntax."""
    print("=== Running Workflow with DSL ===\n")
    
    # Create agents for a research workflow
    researcher = Agent(
        name="researcher",
        system_prompt="You are a research agent. Gather information and provide comprehensive research on the given topic."
    )
    
    analyzer = Agent(
        name="analyzer", 
        system_prompt="You are an analysis agent. Analyze the research data and identify key insights and patterns."
    )
    
    planner = Agent(
        name="planner",
        system_prompt="You are a planning agent. Create actionable plans based on research findings."
    )
    
    synthesizer = Agent(
        name="synthesizer",
        system_prompt="You are a synthesis agent. Combine analysis and planning into a comprehensive report."
    )
    
    # Define workflow using DSL
    # researcher → analyzer, researcher → planner, analyzer&planner → synthesizer
    dsl_dependencies = "researcher→analyzer, researcher→planner, analyzer&planner→synthesizer"
    
    print(f"Using DSL: '{dsl_dependencies}'")
    
    # Parse and show the dependencies
    parsed_deps = parse_dependencies_dsl(dsl_dependencies)
    print(f"Parsed to: {parsed_deps}")
    print()
    
    # Create and run workflow
    workflow = Workflow("research_workflow")
    
    agents = [researcher, analyzer, planner, synthesizer]
    task = "Research the impact of artificial intelligence on education and create an implementation plan."
    
    print("Executing workflow...")
    result = await workflow.run_graph(
        agents=agents,
        dependencies=dsl_dependencies,  # Using DSL directly!
        task=task
    )
    
    print(f"\n✅ Workflow completed in {result.execution_time:.2f} seconds")
    print(f"📊 Execution layers: {result.metadata['total_layers']}")
    print(f"🎯 Final result: {result.result}")


async def compare_dsl_vs_dict():
    """Compare DSL syntax with traditional dictionary syntax."""
    print("=== DSL vs Dictionary Comparison ===\n")
    
    # Complex workflow pattern
    print("Complex workflow: A→B, A→C, B&C→D, A→E, D&E→F")
    print()
    
    # DSL version
    dsl_version = "A→B, A→C, B&C→D, A→E, D&E→F"
    print("DSL Version:")
    print(f"  '{dsl_version}'")
    print()
    
    # Dictionary version
    dict_version = {
        "B": ["A"],
        "C": ["A"], 
        "D": ["B", "C"],
        "E": ["A"],
        "F": ["D", "E"]
    }
    print("Dictionary Version:")
    print(f"  {dict_version}")
    print()
    
    # Verify they're equivalent
    parsed_dsl = parse_dependencies_dsl(dsl_version)
    print("Parsed DSL:")
    print(f"  {parsed_dsl}")
    print()
    
    print(f"Are they equivalent? {parsed_dsl == dict_version}")


async def test_error_handling():
    """Test DSL error handling."""
    print("=== DSL Error Handling ===\n")
    
    # Test invalid DSL strings
    invalid_examples = [
        "A→",                     # Missing target
        "→B",                     # Missing source  
        "A→B→",                   # Incomplete chain
        "A&→B",                   # Empty dependency
        "A→B→C→D→E→F→G→H→I→J",    # Very long chain (should work)
        "A→B, B→C, C→A",          # Circular dependency (will be caught at runtime)
    ]
    
    for dsl in invalid_examples:
        print(f"Testing: '{dsl}'")
        is_valid, error = validate_dsl_syntax(dsl)
        if is_valid:
            try:
                deps = parse_dependencies_dsl(dsl)
                print(f"  ✅ Valid: {deps}")
            except Exception as e:
                print(f"  ⚠️  Parse error: {e}")
        else:
            print(f"  ❌ Invalid: {error}")
        print()


if __name__ == "__main__":
    async def main():
        await demonstrate_dsl_parsing()
        print("\n" + "="*60 + "\n")
        
        await compare_dsl_vs_dict() 
        print("\n" + "="*60 + "\n")
        
        await test_error_handling()
        print("\n" + "="*60 + "\n")
        
        # Note: The actual workflow execution requires API keys
        print("To run the actual workflow example, uncomment the line below")
        print("and ensure you have proper API configuration:")
        print("# await run_workflow_with_dsl()")
    
    asyncio.run(main())
