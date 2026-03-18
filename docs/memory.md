# Memory System

xAgent memory is a minimal long-term memory pipeline.

It does two things:

- extracts memories from unread conversation transcript batches on a delayed schedule
- retrieves relevant memories for the current turn

It also supports one explicit fast path: if a user clearly says "remember this" / "记住这个" / "别忘了", memory extraction runs immediately for the unread message segment.

It does not do query rewriting, keyword tiers, meta-memory extraction, or multi-stage memory fusion.

## Memory Semantics

Memory is agent-scoped and intentionally simple.

That means:

- the agent's full message stream contributes to the same long-term memory pool
- all speakers still contribute to that pool

This keeps cross-turn continuity without adding per-memory visibility logic.

## Quick Start

```python
import asyncio
from xagent.components import MemoryStorageLocal, MessageStorageLocal
from xagent.core import Agent


async def main():
    agent = Agent(
        name="memory_agent",
        model="gpt-5-mini",
        message_storage=MessageStorageLocal(),
        memory_storage=MemoryStorageLocal(collection_name="memory_agent"),
    )

    await agent.chat(
        user_message="Hi, I'm Sarah. I work in data science and I love hiking.",
        user_id="sarah",
        enable_memory=True,
    )

    reply = await agent.chat(
        user_message="Recommend a weekend activity for me.",
        user_id="sarah",
        enable_memory=True,
    )
    print(reply)


asyncio.run(main())
```

## Storage Backends

### Local

```python
from xagent.components import MemoryStorageLocal

memory = MemoryStorageLocal(
    path="./data/chroma",
    collection_name="assistant_memory",
    memory_threshold=10,
)
```

### Extending The Pipeline

If you need a custom backend, keep `MemoryStorageBase` as the minimal interface,
or reuse `MemoryStorageBasic` with your own vector-store implementation.

## Memory Types

The simplified memory pipeline stores:

- `EPISODIC`
- `SEMANTIC`
- `SOCIAL`
- `SELF`

The model is asked to keep:

- `EPISODIC`: dated events, commitments, plans, and decisions
- `SEMANTIC`: stable facts, roles, preferences, and priorities
- `SOCIAL`: relationships, group membership, alignment, and working agreements
- `SELF`: agent-side strategy, work continuity, and response-style adjustments

## API Surface

```python
class MemoryStorageBase(ABC):
    async def add(self, memory_key: str, messages: list[dict]) -> None
    async def store(self, memory_key: str, content: str) -> str | None
    async def retrieve(
        self,
        memory_key: str,
        query: str,
        limit: int = 5,
    ) -> list | None
    async def clear(self, memory_key: str) -> None
    async def delete(self, memory_ids: list[str]) -> None
```

The runtime still resolves `memory_key` to the agent key, and retrieval reads from one shared memory pool for that agent.

## Operational Notes

- Memory is enabled by default and runs unless `enable_memory=False`
- `memory_threshold` now refers to unread message-stream growth, not just user turns
- Writes happen after the configured batch threshold and interval are reached, or immediately when the user explicitly asks the agent to remember something
- Extraction reads only the unread portion of the current message stream and processes it in batches
- Retrieval uses the original user query directly
- Retrieval applies a small relevance threshold before injecting memory back into context
- Writes use lightweight near-duplicate suppression to reduce memory pollution
- Stored memory text keeps timestamps and speaker identifiers so the agent can remember who said what
- Lower thresholds create more memories; higher thresholds create fewer, denser memories
- Larger `history_count` values reduce pressure on memory freshness because the recent message stream stays in model context longer

## Best Practices

- Disable memory explicitly for scenarios that must remain stateless
- Prefer stable `user_id` values because they remain visible in the transcript and extracted memories
- Clear or rotate memory collections when testing different products or tenants
