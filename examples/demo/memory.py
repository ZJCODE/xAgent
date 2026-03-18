"""Enable local long-term memory during normal agent chats."""

import asyncio

from xagent.components import MemoryStorageLocal, MessageStorageLocal
from xagent.core import Agent


async def main():
    message_storage = MessageStorageLocal()
    agent = Agent(
        name="memory_assistant",
        system_prompt="You are a helpful assistant that uses journal memory when available.",
        model="gpt-5-mini",
        message_storage=message_storage,
        memory_storage=MemoryStorageLocal(path=str(message_storage.path)),
    )

    user_id = "alex"

    first_reply = await agent.chat(
        user_message=(
            "Hi, I'm Alex. I lead platform engineering, prefer concise updates, "
            "and I'm planning a Tokyo trip in May."
        ),
        user_id=user_id,
        enable_memory=True,
    )
    print("Turn 1:\n", first_reply, sep="")

    second_reply = await agent.chat(
        user_message="What do you remember about me, and how should you tailor future replies?",
        user_id=user_id,
        enable_memory=True,
    )
    print("\nTurn 2:\n", second_reply, sep="")


if __name__ == "__main__":
    asyncio.run(main())
