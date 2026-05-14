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

Long-term diary memory. The memory package stores and formats durable diary
entries, summaries, and quote-backed people profiles.

- `memory/markdown_memory.py`: markdown file layout, reads, writes, listing, and search.
- `memory/journal_service.py`: LLM formatting for diary entries, summaries, and people profile updates.
