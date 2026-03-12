"""One high-signal workflow example: sequential -> parallel -> sequential."""

import asyncio

from xagent import Agent, Workflow


async def main():
    researcher = Agent(name="Researcher", system_prompt="Collect facts, assumptions, and decision inputs.")
    analyst = Agent(name="Analyst", system_prompt="Turn research into a structured business assessment.")

    finance = Agent(name="Finance", system_prompt="Focus on budget, ROI, and cost control.")
    operations = Agent(name="Operations", system_prompt="Focus on rollout complexity and process changes.")
    support = Agent(name="Support", system_prompt="Focus on customer impact and support load.")

    decision_maker = Agent(name="DecisionMaker", system_prompt="Make a clear recommendation with rationale.")

    stages = [
        {
            "pattern": "sequential",
            "agents": [researcher, analyst],
            "task": "Prepare a decision brief for: {original_task}",
            "name": "research_and_analysis",
        },
        {
            "pattern": "parallel",
            "agents": [finance, operations, support],
            "task": "Review this brief from your perspective: {previous_result}",
            "name": "expert_review",
        },
        {
            "pattern": "sequential",
            "agents": [decision_maker],
            "task": "Write the final recommendation from: {previous_result}",
            "name": "decision",
        },
    ]

    workflow = Workflow("pricing_change_review")
    result = await workflow.run_hybrid(
        task="Should we roll out usage-based pricing for our analytics product in Q3?",
        stages=stages,
        user_id="demo_user",
    )

    print(f"Stages executed: {result['stages_executed']}")
    print(f"Pattern: {' -> '.join(result['stage_patterns'])}")
    print(f"Total execution time: {result['total_execution_time']:.2f}s")
    print("\nFinal result:\n")
    print(result["final_result"])


if __name__ == "__main__":
    asyncio.run(main())
