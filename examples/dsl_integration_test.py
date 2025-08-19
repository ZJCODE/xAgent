#!/usr/bin/env python3
"""
完整的 DSL 集成测试示例
这个示例展示了 DSL 在实际工作流中的使用
"""

import asyncio
import sys
import os

# Add the parent directory to the path so we can import xagent
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xagent.core.agent import Agent
from xagent.multi.workflow import Workflow


async def test_dsl_integration():
    """测试 DSL 与工作流的完整集成"""
    
    print("🔬 DSL Integration Test")
    print("=" * 50)
    
    # 创建测试 agents
    data_collector = Agent(
        name="data_collector",
        system_prompt="You collect and organize data. Respond with structured information."
    )
    
    analyzer = Agent(
        name="analyzer", 
        system_prompt="You analyze data and identify patterns and insights."
    )
    
    planner = Agent(
        name="planner",
        system_prompt="You create actionable plans based on analysis."
    )
    
    report_writer = Agent(
        name="report_writer",
        system_prompt="You write comprehensive reports combining analysis and plans."
    )
    
    # 测试不同的 DSL 模式
    workflow = Workflow("dsl_test_workflow")

    print("\n1️⃣ Testing Simple Chain (A->B->C)")
    print("-" * 30)

    simple_dsl = "data_collector->analyzer->report_writer"
    print(f"DSL: {simple_dsl}")
    
    try:
        result = await workflow.run_graph(
            agents=[data_collector, analyzer, report_writer],
            dependencies=simple_dsl,
            task="Research the benefits of renewable energy"
        )
        print(f"✅ Chain workflow completed in {result.execution_time:.2f}s")
        print(f"📊 Execution layers: {result.metadata['total_layers']}")
        print(f"🎯 Final result preview: {str(result.result)[:100]}...")
    except Exception as e:
        print(f"❌ Error in chain workflow: {e}")

    print("\n2️⃣ Testing Parallel Branches (A->B, A->C, B&C->D)")
    print("-" * 30)

    complex_dsl = "data_collector->analyzer, data_collector->planner, analyzer&planner->report_writer"
    print(f"DSL: {complex_dsl}")
    
    try:
        result = await workflow.run_graph(
            agents=[data_collector, analyzer, planner, report_writer],
            dependencies=complex_dsl,
            task="Analyze renewable energy trends and create implementation strategy"
        )
        print(f"✅ Complex workflow completed in {result.execution_time:.2f}s")
        print(f"📊 Execution layers: {result.metadata['total_layers']}")
        print(f"📋 Layer breakdown:")
        for i, layer in enumerate(result.metadata['execution_layers'], 1):
            print(f"   Layer {i}: {', '.join(layer)}")
        print(f"🎯 Final result preview: {str(result.result)[:100]}...")
    except Exception as e:
        print(f"❌ Error in complex workflow: {e}")
    
    print("\n3️⃣ Testing Hybrid Workflow with DSL")
    print("-" * 30)
    
    stages = [
        {
            "pattern": "sequential",
            "agents": [data_collector, analyzer],
            "task": "Research and analyze: {original_task}",
            "name": "research_phase"
        },
        {
            "pattern": "graph",
            "agents": [planner, report_writer],
            "dependencies": "planner→report_writer",  # Simple DSL in hybrid
            "task": "Create plan and report based on: {previous_result}",
            "name": "planning_phase"
        }
    ]
    
    try:
        result = await workflow.run_hybrid(
            task="Renewable energy adoption strategies",
            stages=stages
        )
        print(f"✅ Hybrid workflow completed in {result['total_execution_time']:.2f}s")
        print(f"📊 Stages executed: {result['stages_executed']}")
        print(f"🔄 Stage patterns: {result['stage_patterns']}")
        print(f"🎯 Final result preview: {str(result['final_result'])[:100]}...")
    except Exception as e:
        print(f"❌ Error in hybrid workflow: {e}")
    
    print("\n4️⃣ DSL vs Dictionary Comparison")
    print("-" * 30)
    
    # 显示等效性
    from xagent.multi.workflow import parse_dependencies_dsl

    test_dsl = "A->B, A->C, B&C->D, A->E, D&E->F"
    expected_dict = {
        "B": ["A"],
        "C": ["A"],
        "D": ["B", "C"],
        "E": ["A"],
        "F": ["D", "E"]
    }
    
    parsed_dsl = parse_dependencies_dsl(test_dsl)
    
    print(f"DSL: '{test_dsl}'")
    print(f"Parsed: {parsed_dsl}")
    print(f"Expected: {expected_dict}")
    print(f"✅ Equivalent: {parsed_dsl == expected_dict}")
    
    print("\n🎉 DSL Integration Test Complete!")


if __name__ == "__main__":
    print("Starting DSL Integration Test...")
    print("Note: This test requires API credentials for full execution")
    print("Set OPENAI_API_KEY environment variable to run with real agents\n")
    
    asyncio.run(test_dsl_integration())
