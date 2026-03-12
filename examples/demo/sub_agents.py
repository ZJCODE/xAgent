"""Compose specialist agents under one coordinator."""

import asyncio

from xagent.components import MessageStorageLocal
from xagent.core import Agent


async def main():
    message_storage = MessageStorageLocal()

    requirements_agent = Agent(
        name="requirements_specialist",
        description="Breaks requests into requirements and success criteria.",
        system_prompt=(
            "You extract requirements, constraints, and open questions. "
            "Focus on clarity and implementation details."
        ),
        model="gpt-4.1-mini",
        message_storage=message_storage,
    )

    writer_agent = Agent(
        name="proposal_writer",
        description="Turns structured inputs into concise internal proposals.",
        system_prompt="You write crisp internal documents with clear actions and tradeoffs.",
        model="gpt-4.1-mini",
        message_storage=message_storage,
    )

    coordinator = Agent(
        name="proposal_coordinator",
        system_prompt=(
            "Delegate requirement extraction and writing work to the specialist agents when useful, "
            "then return one coherent answer."
        ),
        model="gpt-4.1-mini",
        sub_agents=[requirements_agent, writer_agent],
        message_storage=message_storage,
    )

    response = await coordinator.chat(
        user_message=(
            "Create a short internal proposal for moving release notes into a self-serve workflow. "
            "Include scope, likely risks, and a recommendation."
        ),
        user_id="demo_user",
        session_id="sub_agents",
    )
    print(response)


if __name__ == "__main__":
    asyncio.run(main())
