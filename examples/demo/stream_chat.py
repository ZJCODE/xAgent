"""Streaming responses with local message storage."""

import asyncio

from xagent.components import MessageStorageLocal
from xagent.core import Agent


async def main():
    agent = Agent(
        name="streaming_assistant",
        system_prompt="You write practical answers in a calm, direct tone.",
        model="gpt-4.1-mini",
        message_storage=MessageStorageLocal(),
    )

    stream = await agent.chat(
        user_message="Write a short release note announcing faster search and cleaner settings pages.",
        user_id="demo_user",
        session_id="stream_demo",
        stream=True,
    )

    async for chunk in stream:
        print(chunk, end="", flush=True)
    print()


if __name__ == "__main__":
    asyncio.run(main())
