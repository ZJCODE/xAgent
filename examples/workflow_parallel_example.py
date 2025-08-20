"""
Parallel Workflow Example

This example demonstrates how to use the Parallel Workflow pattern
for consensus building, validation, and multi-perspective analysis.

Use cases:
- Critical decisions requiring multiple expert opinions
- Consensus building and error reduction
- Multi-perspective analysis and synthesis
- Quality validation through redundancy
"""

import asyncio
from xagent import Agent
from xagent.multi.workflow import Workflow


async def main():
    # Create specialized agents with different perspectives
    technical_analyst = Agent(
        name="Technical Analyst",
        system_prompt="Analyzes technical feasibility, implementation challenges, and engineering considerations"
    )
    
    business_analyst = Agent(
        name="Business Analyst", 
        system_prompt="Focuses on business impact, market opportunities, and commercial viability"
    )
    
    risk_analyst = Agent(
        name="Risk Analyst",
        system_prompt="Identifies potential risks, compliance issues, and mitigation strategies"
    )
    
    financial_analyst = Agent(
        name="Financial Analyst",
        system_prompt="Evaluates financial implications, costs, ROI, and budget considerations"
    )
    
    # Create workflow orchestrator
    workflow = Workflow(name="multi_perspective_analysis")
    
    # Define a complex decision-making task
    task = """
    Our company is considering implementing a new AI-powered customer service system. 
    Please analyze this proposal and provide recommendations on whether we should proceed.
    
    Key considerations:
    - Initial investment: $500,000
    - Expected to handle 80% of customer inquiries automatically  
    - Current customer service team: 20 people
    - Implementation timeline: 6 months
    - Potential cost savings: $300,000 annually
    """
    
    print("🚀 Starting Parallel Workflow Example")
    print(f"Task: Multi-perspective analysis of AI customer service implementation")
    print("-" * 80)
    
    try:
        # Execute parallel workflow for consensus building
        result = await workflow.run_parallel(
            agents=[technical_analyst, business_analyst, risk_analyst, financial_analyst],
            task=task,
            max_concurrent=4,  # Process all agents concurrently
            user_id="demo_user"
        )
        
        print(f"✅ Workflow completed successfully!")
        print(f"⏱️  Execution time: {result.execution_time:.2f} seconds")
        print(f"🔄 Pattern used: {result.pattern.value}")
        print(f"👥 Agents consulted: {', '.join(result.metadata['agents'])}")
        print(f"🤖 Consensus validator: {result.metadata['consensus_validator']}")
        
        print("\n" + "="*80)
        print("INDIVIDUAL AGENT PERSPECTIVES:")
        print("="*80)
        
        # Show each agent's perspective
        for agent_name, agent_result in result.metadata['worker_results'].items():
            print(f"\n🔍 {agent_name.upper()} PERSPECTIVE:")
            print("-" * 50)
            print(agent_result)
        
        print("\n" + "="*80)
        print("CONSENSUS ANALYSIS & FINAL RECOMMENDATION:")
        print("="*80)
        print(result.result)
        
    except Exception as e:
        print(f"❌ Workflow failed: {e}")


async def simple_consensus_example():
    """
    A simpler example showing consensus building with identical agents
    for validation and error reduction.
    """
    print("\n" + "🔄 Simple Consensus Example")
    print("-" * 50)
    
    # Create multiple instances of similar agents for consensus
    solver1 = Agent(name="Math Solver 1", system_prompt="Mathematical problem solver")
    solver2 = Agent(name="Math Solver 2", system_prompt="Mathematical problem solver") 
    solver3 = Agent(name="Math Solver 3", system_prompt="Mathematical problem solver")
    
    workflow = Workflow(name="math_consensus")
    
    task = "Solve this problem step by step: If a train travels 120 km in 2 hours, and then 180 km in the next 3 hours, what is the average speed for the entire journey?"
    
    result = await workflow.run_parallel(
        agents=[solver1, solver2, solver3],
        task=task,
        user_id="demo_user"
    )
    
    print(f"✅ Consensus reached in {result.execution_time:.2f} seconds")
    print("\nFinal Answer:")
    print(result.result)


if __name__ == "__main__":
    # Run the main multi-perspective example
    # asyncio.run(main())
    
    # Run the simple consensus example
    asyncio.run(simple_consensus_example())
