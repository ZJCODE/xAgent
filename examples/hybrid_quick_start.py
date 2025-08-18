"""
5-Minute Hybrid Workflow Tutorial
=================================

This is the fastest way to understand and use run_hybrid.
Copy this code and run it to see hybrid workflows in action!
"""

import asyncio
from xagent.core.agent import Agent
from xagent.multi.workflow import Workflow

async def quick_start():
    """
    3-Stage Business Decision Workflow
    =================================
    
    Real scenario: "Should we expand to international markets?"
    
    Stage 1: Research (Sequential)
    Stage 2: Expert Analysis (Parallel) 
    Stage 3: Decision (Sequential)
    """
    
    # Create agents (think of them as consultants)
    researcher = Agent("Market Researcher", "Researches market conditions")
    analyst = Agent("Business Analyst", "Analyzes business feasibility")
    
    finance_expert = Agent("CFO", "Financial analysis and projections")
    operations_expert = Agent("COO", "Operational challenges and logistics")
    marketing_expert = Agent("CMO", "Marketing strategy and positioning")
    
    decision_maker = Agent("CEO", "Makes final strategic decisions")
    
    # Design the workflow stages
    stages = [
        # Stage 1: Sequential Research
        {
            "pattern": "sequential",  # researcher → analyst
            "agents": [researcher, analyst],
            "task": "Research international expansion for: {original_task}",
            "name": "research"
        },
        
        # Stage 2: Parallel Expert Analysis  
        {
            "pattern": "parallel",   # All experts work simultaneously
            "agents": [finance_expert, operations_expert, marketing_expert],
            "task": "Analyze this research from your expertise: {previous_result}",
            "name": "expert_analysis"
        },
        
        # Stage 3: Final Decision
        {
            "pattern": "sequential",  # Single decision maker
            "agents": [decision_maker],
            "task": "Make final decision based on expert analysis: {previous_result}",
            "name": "final_decision"
        }
    ]
    
    # Execute the hybrid workflow
    workflow = Workflow("international_expansion")
    
    result = await workflow.run_hybrid(
        stages=stages,
        task="Should our tech startup expand internationally?",
        user_id="ceo"
    )
    
    # Show results
    print("🎯 BUSINESS DECISION COMPLETE!")
    print("=" * 40)
    print(f"⏱️ Total time: {result['total_execution_time']:.1f} seconds")
    print(f"🏗️ Stages: {result['stages_executed']}")
    print(f"📊 Pattern: {' → '.join(result['stage_patterns'])}")
    print()
    print("🎯 FINAL DECISION:")
    print(result['final_result'])
    
    return result

async def main():
    print("🚀 5-Minute Hybrid Workflow Tutorial")
    print("Business Question: Should our tech startup expand internationally?")
    print()
    
    result = await quick_start()
    
    print("\n✅ Tutorial Complete!")
    print("\n💡 What happened:")
    print("1. Researcher → Analyst (Sequential: context building)")
    print("2. CFO + COO + CMO (Parallel: multiple expert perspectives)")  
    print("3. CEO (Sequential: final decision)")
    print("\n🎉 You just ran a hybrid workflow!")

if __name__ == "__main__":
    # Just run this file!
    asyncio.run(main())
