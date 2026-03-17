# Memory System

xAgent memory is a minimal long-term memory pipeline.

It does two things:

- extracts memories from conversation history after a user-turn threshold is reached
- retrieves relevant memories for the current turn

It also supports one explicit fast path: if a user clearly says "remember this" / "记住这个" / "别忘了", memory extraction runs immediately for the unread transcript segment.

It does not do query rewriting, keyword tiers, meta-memory extraction, or multi-stage memory fusion.

## Memory Semantics

Memory is agent-global.

That means:

- all conversations contribute to the same long-term memory pool for the agent
- all speakers contribute to that same pool
- retrieved memory can cross conversation and cross user boundaries

This is a deliberate product choice.

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
        conversation_id="intro",
        enable_memory=True,
    )

    reply = await agent.chat(
        user_message="Recommend a weekend activity for me.",
        user_id="bob",
        conversation_id="team_chat",
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

### Cloud

```python
from xagent.components import MemoryStorageCloud

memory = MemoryStorageCloud(
    memory_threshold=10,
)
```

Cloud mode requires:

```bash
export UPSTASH_VECTOR_REST_URL=https://your-database.upstash.io
export UPSTASH_VECTOR_REST_TOKEN=your_token_here
```

## Memory Types

The simplified memory pipeline stores:

- `PROFILE`
- `EPISODIC`

It does not store `META` memories.

## API Surface

```python
class MemoryStorageBase(ABC):
    async def add(self, memory_key: str, conversation_id: str, messages: list[dict]) -> None
    async def store(self, memory_key: str, content: str) -> str | None
    async def retrieve(self, memory_key: str, query: str, limit: int = 5) -> list | None
    async def clear(self, memory_key: str) -> None
    async def delete(self, memory_ids: list[str]) -> None
```

The runtime always resolves `memory_key` to the agent-global key.

## Operational Notes

- Memory is enabled by default and runs unless `enable_memory=False`
- `memory_threshold` counts user turns, not assistant replies
- Writes happen after the configured threshold is reached, or immediately when the user explicitly asks the agent to remember something
- Extraction reads only the unread portion of the current conversation transcript
- Retrieval uses the original user query directly
- Retrieval applies a small relevance threshold before injecting memory back into context
- Writes use lightweight near-duplicate suppression to reduce memory pollution
- Stored memory text keeps speaker identifiers so the agent can remember who said what
- Lower thresholds create more memories; higher thresholds create fewer, denser memories

## Best Practices

- Disable memory explicitly for scenarios that must remain stateless
- Assume memory can surface context across users and conversations
- Clear or rotate memory collections when testing different products or tenants
