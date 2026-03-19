# Memory System

xAgent memory is now a SQLite-backed daily journal system.

It does two things:

- rewrites per-day journal entries from unread conversation transcript batches
- retrieves relevant journal entries for the current turn

It still supports one explicit fast path: if a user clearly says "remember this" / "记住这个" / "别忘了", the current unread batch is journaled immediately.

## Memory Semantics

Journal memory is agent-scoped and intentionally simple.

- the agent's full message stream contributes to one shared journal stream
- all speakers still contribute to that journal stream
- each `memory_key + YYYY-MM-DD` has one journal row whose `content` is rewritten in place during the day

This keeps cross-turn continuity without introducing separate memory collections or per-memory visibility rules.

## Quick Start

```python
import asyncio
from xagent.components import MemoryStorageLocal, MessageStorageLocal
from xagent.core import Agent


async def main():
    message_storage = MessageStorageLocal()
    agent = Agent(
        name="memory_agent",
        model="gpt-5-mini",
        message_storage=message_storage,
        memory_storage=MemoryStorageLocal(path=str(message_storage.path)),
    )

    await agent.chat(
        user_message="Hi, I'm Sarah. I work in data science and I love hiking.",
        user_id="sarah",
        enable_memory=True,
    )

    reply = await agent.chat(
        user_message="What do you remember about me?",
        user_id="sarah",
        enable_memory=True,
    )
    print(reply)


asyncio.run(main())
```

## Storage Layout

Local memory uses the same SQLite file as message history.

```python
from xagent.components import MemoryStorageLocal

memory = MemoryStorageLocal(
    path="./data/assistant_messages.sqlite3",
    memory_threshold=10,
)
```

The database contains:

- `messages`: the continuous agent-level message stream
- `journals`: one row per `memory_key + journal_date`
- `journal_state`: persistent unread cursor and last-write timestamp
- `journal_fts`: FTS5 trigram index used for keyword retrieval

## Retrieval Model

- normal chat turns automatically inject only the most recent daily journal context
- keyword journal search is exposed as the `search_journal_memory` tool and should be triggered only when older memory is actually needed
- `date` only: exact journal lookup for one day
- `query` only: LLM extracts 3-5 keywords, then journal retrieval searches SQLite FTS5
- `date + query`: keyword retrieval scoped to the specified date
- short keywords fall back to `LIKE` matching so short Chinese words still match

## API Surface

```python
class MemoryStorageBase(ABC):
    async def add(self, memory_key: str, messages: list[dict]) -> None
    async def store(self, memory_key: str, content: str) -> str | None
    async def retrieve(
        self,
        memory_key: str,
        query: str = "",
        limit: int = 5,
        journal_date: str | None = None,
    ) -> list | None
    async def clear(self, memory_key: str) -> None
    async def delete(self, memory_ids: list[str]) -> None
```

## Operational Notes

- Memory is enabled by default and runs unless `enable_memory=False`
- `memory_threshold` refers to unread message-stream growth, not just user turns
- Writes happen after the configured batch threshold and interval are reached, or immediately when the user explicitly asks the agent to remember something
- Journal writes are message-driven background tasks; there is no independent daemon timer
- `last_processed_message_id` is persisted in SQLite so restarts do not re-journal old messages
- Recent journal context is injected into the system prompt as date-stamped long-term context
- Full keyword-based journal search is on-demand through the memory tool, which avoids paying that cost on every turn

## Best Practices

- Disable memory explicitly for scenarios that must remain stateless
- Prefer stable `user_id` values because they remain visible in the transcript and daily journal
- Keep the message and memory backends on the same SQLite file unless you are implementing a custom backend on purpose
