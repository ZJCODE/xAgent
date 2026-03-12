"""Extract higher-level meta memories from local memory storage."""

import asyncio

from xagent.components import MemoryStorageLocal


MEMORIES = [
    """
    User: I'm Priya. I lead product operations and prefer short written updates.
    Assistant: Noted. I'll keep updates concise and action-oriented.
    """,
    """
    User: I'm preparing a cross-team launch review for next month and need tighter follow-up habits.
    Assistant: Let's create a recurring checklist and weekly review cadence.
    """,
    """
    User: I usually go for a long run on Saturday mornings because it helps me reset after busy weeks.
    Assistant: That's a strong routine for managing stress and keeping energy stable.
    """,
    """
    User: I skipped lunch twice this week and felt scattered in the afternoon.
    Assistant: That suggests your focus drops when work crowds out your normal routine.
    """,
]


async def main():
    memory = MemoryStorageLocal(collection_name="demo_meta_memory")
    user_id = "meta_demo_user"

    print("Storing source memories...")
    for index, content in enumerate(MEMORIES, start=1):
        memory_id = await memory.store(user_id, content)
        print(f"{index}. stored -> {memory_id}")

    print("\nRetrieval before meta extraction:")
    before = await memory.retrieve(user_id, "What do we know about Priya's routines?", limit=3)
    for item in before:
        print(f"- {item['metadata']['memory_type']}: {item['content'][:100]}...")

    meta_ids = await memory.extract_meta(user_id, days=1)
    print(f"\nExtracted meta memory ids: {meta_ids}")

    print("\nRetrieval after meta extraction:")
    after = await memory.retrieve(
        user_id,
        "Summarize Priya's work style, stress patterns, and personal routines.",
        limit=5,
    )
    for item in after:
        memory_type = item["metadata"]["memory_type"]
        print(f"- {memory_type}: {item['content'][:160]}...")


if __name__ == "__main__":
    asyncio.run(main())
