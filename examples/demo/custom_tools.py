"""Register sync and async local tools with an agent."""

import asyncio

from xagent.components import MessageStorageLocal
from xagent.core import Agent
from xagent.utils import function_tool


@function_tool()
def estimate_project_cost(hours: int, hourly_rate: int) -> str:
    """Estimate project cost from effort and rate."""
    total = hours * hourly_rate
    return f"Estimated cost: ${total} for {hours} hours at ${hourly_rate}/hour."


@function_tool()
def title_case(text: str) -> str:
    """Convert text to title case."""
    return text.title()


@function_tool()
async def assess_timeline_risk(scope: str, deadline_days: int) -> str:
    """Return a simple schedule risk assessment."""
    await asyncio.sleep(0.1)
    if deadline_days <= 7:
        risk = "high"
    elif deadline_days <= 21:
        risk = "medium"
    else:
        risk = "low"
    return f"Timeline risk for {scope!r}: {risk}."


async def main():
    agent = Agent(
        name="tool_demo",
        model="gpt-5-mini",
        tools=[estimate_project_cost, title_case, assess_timeline_risk],
        message_storage=MessageStorageLocal(),
    )

    response = await agent.chat(
        user_message=(
            "Estimate the cost of a 36-hour onboarding project at 120 dollars per hour, "
            "convert 'q2 platform refresh' to title case, and assess the risk for shipping it in 10 days."
        ),
        user_id="demo_user",
        session_id="tool_demo",
    )
    print(response)


if __name__ == "__main__":
    asyncio.run(main())
