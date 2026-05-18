"""Dedicated tools for long-term memory access."""

from __future__ import annotations

from typing import TYPE_CHECKING

from xagent.utils.tool_decorator import function_tool

if TYPE_CHECKING:
    from xagent.components.memory import SQLiteMemory
    from xagent.components.message import MessageStorageBase


def create_write_memory_tool(
    memory: SQLiteMemory,
    is_enabled,
):
    """Create a tool that records long-term useful memory."""

    @function_tool(
        name="write_memory",
        description=(
            "Record long-term useful memory from the conversation. "
            "Use this for stable preferences, important facts, decisions, commitments, "
            "or notable context that should help future interactions. "
            "Keep entries concise, factual, and clearly attributed when needed."
        ),
        param_descriptions={
            "content": "The memory note to record. Keep it concise, useful, and grounded in the conversation.",
        },
    )
    async def write_memory(content: str) -> dict:
        """Record a long-term memory note."""
        if not is_enabled():
            return {"status": "disabled", "message": "Memory writing is disabled for this turn."}

        content = content.strip()
        if not content:
            return {"status": "skipped", "message": "Empty content, nothing written."}

        await memory.add_entry(content, source="tool")
        return {"status": "ok", "message": "Memory recorded."}

    return write_memory


MEMORY_SQL_SCHEMA = """
Memory database schema:
- memory_entries(id INTEGER, entry_date TEXT YYYY-MM-DD, created_at REAL unix timestamp,
  source TEXT such as 'tool' or 'auto_diary', content TEXT, metadata_json TEXT)
- memory_summaries(id INTEGER, period_type TEXT: weekly/monthly/yearly,
  period_start TEXT YYYY-MM-DD, period_end TEXT YYYY-MM-DD, generated_at REAL,
  content TEXT, metadata_json TEXT)
- people_facts(id INTEGER, person_key TEXT, display_name TEXT, fact TEXT,
  evidence TEXT, source TEXT, observed_at TEXT YYYY-MM-DD, created_at REAL,
  metadata_json TEXT)
Recommended queries:
- Recent memory: SELECT entry_date, source, content FROM memory_entries ORDER BY entry_date DESC, id DESC LIMIT 20
- Topic recall: SELECT entry_date, source, content FROM memory_entries WHERE content LIKE '%keyword%' ORDER BY entry_date DESC LIMIT 20
- Person recall: SELECT display_name, fact, evidence, observed_at FROM people_facts WHERE person_key LIKE '%name%' OR display_name LIKE '%name%' ORDER BY observed_at DESC LIMIT 20
"""


MESSAGES_SQL_SCHEMA = """
Messages database schema:
- messages(id INTEGER, timestamp REAL unix timestamp, role TEXT, type TEXT,
  sender_id TEXT, content TEXT, metadata_json TEXT, tool_call_json TEXT,
  multimodal_json TEXT, message_json TEXT)
Message meaning:
- role is user/assistant/system/tool/environment.
- type is message/context_event/function_call/function_call_output.
- sender_id identifies the speaker when available; assistant replies usually use sender_id='agent'.
- message_json is the full persisted Message object for exact reconstruction.
Recommended queries:
- Recent older messages: SELECT datetime(timestamp, 'unixepoch') AS time, role, sender_id, content FROM messages ORDER BY id DESC LIMIT 50
- User/topic recall: SELECT datetime(timestamp, 'unixepoch') AS time, sender_id, content FROM messages WHERE content LIKE '%keyword%' ORDER BY id DESC LIMIT 50
"""


def create_query_memory_tool(
    memory: SQLiteMemory,
    is_enabled,
):
    """Create a tool for read-only SQL querying of long-term memory."""

    @function_tool(
        name="query_memory",
        description=(
            "Run a read-only SQL SELECT query against long-term memory for normal recall. "
            "Use this for past preferences, plans, durable facts, summaries, and people facts "
            "that are not already available in recent memory context. "
            "Only SELECT or WITH queries are allowed. "
            + MEMORY_SQL_SCHEMA
        ),
        param_descriptions={
            "sql": "Single read-only SELECT/WITH SQL statement against the memory database.",
            "max_rows": "Maximum rows to return. Default: 50; hard limit: 200.",
        },
    )
    async def query_memory(
        sql: str,
        max_rows: int = 50,
    ) -> dict:
        """Query long-term memory with safe read-only SQL."""
        if not is_enabled():
            return {"status": "disabled", "enabled": False, "message": "Memory reading is disabled for this turn."}
        try:
            result = await memory.query_sql(sql, max_rows=max_rows)
            result["enabled"] = True
            return result
        except Exception as exception:
            return {"status": "error", "enabled": True, "message": str(exception)}

    return query_memory


def create_query_messages_tool(
    message_storage: "MessageStorageBase",
    is_enabled,
):
    """Create a tool for deep read-only SQL querying of persisted messages."""

    @function_tool(
        name="query_messages",
        description=(
            "Run a read-only SQL SELECT query against the full persisted message history. "
            "This is a deep recall tool. Use it only when the user explicitly asks you to "
            "carefully remember, review older conversation history in detail, or when "
            "`query_memory` is insufficient. Recent messages are already in context, so do "
            "not call this for ordinary recall. Only SELECT or WITH queries are allowed. "
            + MESSAGES_SQL_SCHEMA
        ),
        param_descriptions={
            "sql": "Single read-only SELECT/WITH SQL statement against the messages database.",
            "max_rows": "Maximum rows to return. Default: 50; hard limit: 200.",
        },
    )
    async def query_messages(
        sql: str,
        max_rows: int = 50,
    ) -> dict:
        """Query persisted message history with safe read-only SQL."""
        if not is_enabled():
            return {"status": "disabled", "enabled": False, "message": "Memory reading is disabled for this turn."}

        query_sql = getattr(message_storage, "query_sql", None)
        if query_sql is None:
            return {
                "status": "unsupported",
                "enabled": True,
                "message": "The active message storage does not support SQL queries.",
            }
        try:
            result = await query_sql(sql, max_rows=max_rows)
            result["enabled"] = True
            return result
        except Exception as exception:
            return {"status": "error", "enabled": True, "message": str(exception)}

    return query_messages
