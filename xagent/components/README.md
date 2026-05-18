# Components

`xagent.components` contains infrastructure components used by the agent runtime.
It does not decide agent behavior; higher layers in `xagent.core` coordinate
when these components are used.

## Message

Short-term conversation history. The message package stores ordered `Message`
objects for the current agent stream.

- `message/base.py`: storage interface and shared input validation.
- `message/local.py`: SQLite-backed persistent message history.
- `message/private_temp.py`: temporary private-mode message history.

## Memory

Long-term memory. The memory package stores and formats durable memory entries,
runtime-maintained summaries, and quote-backed people profiles.

- `memory/sqlite_memory.py`: SQLite storage for memory entries, summaries, people facts, and read-only SQL querying.
- `memory/journal_service.py`: LLM formatting for memory entries, summaries, and people profile updates.
