#!/usr/bin/env python3
"""
Auto Workflow Demo - Demonstrates the intelligent automatic workflow generation feature.

This example showcases how xAgent can automatically:
1. Analyze task complexity
2. Generate optimal number of agents (2-6) with specialized roles
3. Create optimal dependency patterns using Dict[str, List[str]] format
4. Execute the workflow with parallel optimization

The auto workflow uses structured output types to ensure reliable generation.
"""

import asyncio
import json
import logging
from pathlib import Path
import sys
import os

# Add parent directory to path for imports
current_dir = Path(__file__).parent
project_root = current_dir.parent.parent
sys.path.insert(0, str(project_root))

from xagent.core.agent import Agent
from xagent.multi.workflow import Workflow

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def demo_simple_auto_workflow():
    """Demo auto workflow with a simple task."""
    print("🚀 Demo 1: Simple Task - Blog Post Creation")
    print("=" * 60)
    
    workflow = Workflow(name="simple_auto_demo")
    
    task = """Create a comprehensive blog post about the benefits of renewable energy. 
    The post should include research, analysis, and actionable recommendations for readers."""
    
    try:
        result = await workflow.run_auto(
            task=task,
            user_id="demo_user_1"
        )
        
        print(f"✅ Auto workflow completed in {result.execution_time:.2f}s")
        print(f"📊 Generated {result.metadata['agent_count']} agents")
        print(f"🧠 Agent selection reasoning: {result.metadata['agent_selection_reasoning']}")
        print(f"🔗 Generated dependencies: {result.metadata['generated_dependencies']}")
        print(f"💡 Dependencies explanation: {result.metadata['dependencies_explanation']}")
        
        print("\n📝 Generated Agents:")
        for i, agent_spec in enumerate(result.metadata['generated_agents'], 1):
            print(f"{i}. {agent_spec['name']}")
            print(f"   Role: {agent_spec['system_prompt'][:100]}...")
        
        print(f"\n🎯 Final Result:")
        print(f"{str(result.result)[:300]}...")
        
        return result
        
    except Exception as e:
        logger.error(f"Simple auto workflow failed: {e}")
        raise


async def demo_complex_auto_workflow():
    """Demo auto workflow with a complex multi-domain task."""
    print("\n\n🚀 Demo 2: Complex Task - AI Strategy Development")
    print("=" * 60)
    
    workflow = Workflow(name="complex_auto_demo")
    
    task = """Develop a comprehensive AI implementation strategy for a mid-size manufacturing company. 
    The strategy should include:
    - Current state analysis and readiness assessment
    - Technology evaluation and selection
    - Implementation roadmap with phases
    - Risk assessment and mitigation strategies
    - ROI projections and business case
    - Change management and training plans
    - Compliance and governance frameworks
    
    Consider industry best practices, regulatory requirements, and company-specific factors."""
    
    try:
        result = await workflow.run_auto(
            task=task,
            user_id="demo_user_2"
        )
        
        print(f"✅ Complex auto workflow completed in {result.execution_time:.2f}s")
        print(f"📊 Generated {result.metadata['agent_count']} agents")
        print(f"🧠 Agent selection reasoning: {result.metadata['agent_selection_reasoning']}")
        
        print("\n🔗 Dependencies Structure:")
        deps = result.metadata['generated_dependencies']
        for agent, dependencies in deps.items():
            if dependencies:
                print(f"   {agent} ← depends on: {dependencies}")
        
        print(f"\n💡 Dependencies explanation: {result.metadata['dependencies_explanation']}")
        
        print("\n📝 Generated Agents:")
        for i, agent_spec in enumerate(result.metadata['generated_agents'], 1):
            print(f"{i}. {agent_spec['name']}")
            print(f"   Role: {agent_spec['system_prompt'][:150]}...")
        
        print(f"\n⏱️ Timing Breakdown:")
        print(f"   Agent generation: {result.metadata['agent_generation_time']:.2f}s")
        print(f"   Dependencies generation: {result.metadata['dependencies_generation_time']:.2f}s")
        print(f"   Execution: {result.execution_time:.2f}s")
        print(f"   Total: {result.metadata['total_auto_time']:.2f}s")
        
        print(f"\n📊 Execution Details:")
        print(f"   Total layers: {result.metadata['total_layers']}")
        print(f"   Execution layers: {result.metadata['execution_layers']}")
        
        print(f"\n🎯 Final Result:")
        print(f"{str(result.result)[:500]}...")
        
        return result
        
    except Exception as e:
        logger.error(f"Complex auto workflow failed: {e}")
        raise


async def demo_technical_auto_workflow():
    """Demo auto workflow with a technical development task."""
    print("\n\n🚀 Demo 3: Technical Task - Software Architecture Design")
    print("=" * 60)
    
    workflow = Workflow(name="technical_auto_demo")
    
    task = """Design a scalable microservices architecture for a real-time financial trading platform. 
    Requirements:
    - Handle 100,000+ transactions per second
    - Sub-millisecond latency for critical operations
    - 99.99% uptime requirement
    - Support for multiple asset classes (stocks, forex, crypto)
    - Real-time market data processing
    - Compliance with financial regulations
    - Multi-region deployment capability
    - Comprehensive monitoring and alerting
    
    Provide detailed architecture diagrams, technology stack recommendations, 
    data flow designs, and implementation guidelines."""
    
    try:
        result = await workflow.run_auto(
            task=task,
            user_id="demo_user_3"
        )
        
        print(f"✅ Technical auto workflow completed in {result.execution_time:.2f}s")
        print(f"📊 Generated {result.metadata['agent_count']} specialized agents")
        
        print("\n🔗 Workflow Dependencies Graph:")
        deps = result.metadata['generated_dependencies']
        # Create a simple visualization of the dependency graph
        all_agents = set()
        for agent, dependencies in deps.items():
            all_agents.add(agent)
            all_agents.update(dependencies)
        
        # Find root agents (no dependencies)
        root_agents = all_agents - set(deps.keys())
        print(f"   Root agents (no dependencies): {list(root_agents)}")
        
        for agent, dependencies in deps.items():
            print(f"   {agent} ← {dependencies}")
        
        print("\n📝 Specialized Agents Created:")
        for i, agent_spec in enumerate(result.metadata['generated_agents'], 1):
            print(f"{i}. {agent_spec['name']}")
            print(f"   Expertise: {agent_spec['system_prompt'][:120]}...")
        
        print(f"\n🎯 Architecture Result Preview:")
        result_str = str(result.result)
        if len(result_str) > 800:
            print(f"{result_str[:400]}...")
            print(f"[... {len(result_str)-800} more characters ...]")
            print(f"...{result_str[-400:]}")
        else:
            print(result_str)
        
        return result
        
    except Exception as e:
        logger.error(f"Technical auto workflow failed: {e}")
        raise


async def demo_auto_workflow_comparison():
    """Compare auto workflow with manual workflow for the same task."""
    print("\n\n🔍 Demo 4: Auto vs Manual Workflow Comparison")
    print("=" * 60)
    
    task = "Create a marketing strategy for launching a new sustainable product line."
    
    # Manual workflow setup
    print("🛠️ Manual Workflow:")
    researcher = Agent(
        name="market_researcher",
        system_prompt="You are a market research specialist. Analyze market trends, competitor landscape, and customer needs."
    )
    strategist = Agent(
        name="marketing_strategist", 
        system_prompt="You are a marketing strategy expert. Develop comprehensive marketing plans and positioning strategies."
    )
    analyst = Agent(
        name="data_analyst",
        system_prompt="You are a data analyst. Analyze market data and provide insights for strategic decisions."
    )
    
    manual_workflow = Workflow(name="manual_comparison")
    
    # Manual dependencies
    manual_deps = {
        "marketing_strategist": ["market_researcher"],
        "data_analyst": ["market_researcher"]
    }
    
    print(f"   Agents: {len([researcher, strategist, analyst])} (manually designed)")
    print(f"   Dependencies: {manual_deps}")
    
    # Auto workflow
    print("\n🤖 Auto Workflow:")
    auto_workflow = Workflow(name="auto_comparison")
    
    # Run both workflows
    try:
        # Manual workflow
        manual_start = asyncio.get_event_loop().time()
        manual_result = await manual_workflow.run_graph(
            agents=[researcher, strategist, analyst],
            dependencies=manual_deps,
            task=task,
            user_id="comparison_user"
        )
        manual_time = asyncio.get_event_loop().time() - manual_start
        
        # Auto workflow  
        auto_start = asyncio.get_event_loop().time()
        auto_result = await auto_workflow.run_auto(
            task=task,
            user_id="comparison_user"
        )
        auto_time = asyncio.get_event_loop().time() - auto_start
        
        print(f"   Agents: {auto_result.metadata['agent_count']} (automatically generated)")
        print(f"   Dependencies: {auto_result.metadata['generated_dependencies']}")
        
        print("\n📊 Comparison Results:")
        print(f"   Manual workflow time: {manual_time:.2f}s")
        print(f"   Auto workflow time: {auto_time:.2f}s")
        print(f"   Auto generation overhead: {auto_result.metadata['agent_generation_time'] + auto_result.metadata['dependencies_generation_time']:.2f}s")
        
        print(f"\n📈 Auto Workflow Advantages:")
        print(f"   - Dynamic agent count based on task complexity")
        print(f"   - Optimized dependencies for parallel execution")
        print(f"   - Specialized roles automatically determined")
        print(f"   - No manual dependency design required")
        
        return {"manual": manual_result, "auto": auto_result}
        
    except Exception as e:
        logger.error(f"Workflow comparison failed: {e}")
        raise

async def main():
    """Run all auto workflow demos."""
    print("🎭 xAgent Auto Workflow Demo Suite")
    print("=" * 60)
    print("This demo showcases intelligent automatic workflow generation:")
    print("• Dynamic agent creation based on task complexity")
    print("• Automatic dependency pattern optimization") 
    print("• Structured output types for reliable generation")
    print("• Parallel execution with optimal resource utilization")
    
    results = {}
    
    try:
        # Run demos
        results["simple_task"] = await demo_simple_auto_workflow()
        # results["complex_task"] = await demo_complex_auto_workflow()
        # results["technical_task"] = await demo_technical_auto_workflow()
        # results["comparison"] = await demo_auto_workflow_comparison()
        
        print("\n\n🎉 All Auto Workflow Demos Completed Successfully!")
        print("=" * 60)
        print("Key Benefits Demonstrated:")
        print("✅ Intelligent task complexity analysis")
        print("✅ Optimal agent count determination (2-6 agents)")
        print("✅ Automatic dependency optimization")
        print("✅ Structured output with type constraints")
        print("✅ Parallel execution with layer-based optimization")
        print("✅ Comprehensive metadata and timing information")
        
    except Exception as e:
        logger.error(f"Demo suite failed: {e}")
        print(f"\n❌ Demo failed: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
