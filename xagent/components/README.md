# Components

`xagent.components` contains infrastructure components used by the agent runtime.
It does not decide agent behavior; higher layers in `xagent.core` coordinate
when these components are used.

## Message

Short-term conversation history. The message package now delegates persistent
local storage to the unified experience memory database while keeping the
`MessageStorageBase` interface for handlers.

- `message/base.py`: storage interface and shared input validation.
- `message/local.py`: default local adapter backed by `memory/xagent_memory.sqlite3`.
- `message/private_temp.py`: temporary private-mode message history.

## Memory

Long-term memory. The memory package stores raw events, durable memory items,
evidence, revisions, people, summaries, retention policies, and read-only
debug querying.

- `memory/experience_store.py`: canonical SQLite + FTS5 store for events and memory.
- `memory/journal_service.py`: LLM formatting for memory entries, summaries, and people profile updates.
- `memory/services.py`: service boundaries for extraction, retrieval, reconciliation, summaries, and retention.
