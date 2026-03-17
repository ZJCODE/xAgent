"""Local-first Python API quick start."""

import asyncio

from xagent.components import MessageStorageLocal
from xagent.core import Agent


async def main():
    # Use explicit local storage so the persistence mode is obvious in the demo.
    message_storage = MessageStorageLocal()
    agent = Agent(
        name="project_assistant",
        system_prompt="You are a concise assistant for internal project planning.",
        model="gpt-5-mini",
        message_storage=message_storage,
    )

    user_id = "demo_user"
    planning_conversation = "roadmap_planning"

    reply = await agent.chat(
        user_message="We are launching a private beta in six weeks. Give me a three-item kickoff checklist.",
        user_id=user_id,
        conversation_id=planning_conversation,
    )
    print("Turn 1:\n", reply, sep="")

    follow_up = await agent.chat(
        user_message="Now turn that checklist into a short status update for the team.",
        user_id=user_id,
        conversation_id=planning_conversation,
    )
    print("\nTurn 2:\n", follow_up, sep="")

    separate_conversation = await agent.chat(
        user_message="In a separate conversation, suggest a title for a product retrospective.",
        user_id=user_id,
        conversation_id="retrospective",
    )
    print("\nIndependent conversation:\n", separate_conversation, sep="")


if __name__ == "__main__":
    asyncio.run(main())
