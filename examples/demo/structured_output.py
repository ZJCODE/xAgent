"""Typed responses with Pydantic output models."""

import asyncio

from pydantic import BaseModel

from xagent.components import MessageStorageLocal
from xagent.core import Agent


class LaunchPlan(BaseModel):
    audience: str
    goal: str
    milestones: list[str]
    risks: list[str]


class Step(BaseModel):
    explanation: str
    output: str


class MathReasoning(BaseModel):
    steps: list[Step]
    final_answer: str


async def main():
    agent = Agent(
        model="gpt-4.1-mini",
        output_type=LaunchPlan,
        message_storage=MessageStorageLocal(),
    )

    launch_plan = await agent.chat(
        user_message=(
            "Create a lightweight launch plan for releasing an internal analytics dashboard to 20 beta users."
        ),
        user_id="demo_user",
        session_id="structured_output",
    )

    print("Launch plan:")
    print(launch_plan.model_dump_json(indent=2))

    reasoning = await agent.chat(
        user_message="Solve 8x + 7 = -23 step by step.",
        user_id="demo_user",
        session_id="structured_output",
        output_type=MathReasoning,
    )

    print("\nMath reasoning:")
    print(reasoning.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())
