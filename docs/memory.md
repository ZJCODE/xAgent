# Memory System

xAgent memory is a minimal long-term memory pipeline.

It does two things:

- extracts memories from conversation history after a threshold is reached
- retrieves relevant memories for the current turn

It does not do query rewriting, meta-memory extraction, or multi-stage memory fusion.

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
    keep_recent=2,
)
```

### Cloud

```python
from xagent.components import MemoryStorageCloud

memory = MemoryStorageCloud(
    memory_threshold=10,
    keep_recent=2,
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

- Memory only runs when `enable_memory=True`
- Writes happen after the configured threshold is reached
- Retrieval uses the original user query directly
- Stored memory text keeps speaker identifiers so the agent can remember who said what
- Lower thresholds create more memories; higher thresholds create fewer, denser memories

## Best Practices

- Enable memory only where continuity matters
- Assume memory can surface context across users and conversations
- Clear or rotate memory collections when testing different products or tenants
