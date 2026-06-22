"""Dedicated tools for long-term memory access."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from xagent.tools.protocol import function_tool

if TYPE_CHECKING:
    from xagent.ports import MemoryStore, MessageStore


def create_write_memory_tool(
    memory: MemoryStore,
    is_enabled: bool = True,
):
    """Create a tool that records long-term useful memory."""

    @function_tool(
        name="write_memory",
        description=(
            "Record a concise, attributable diary-memory note for durable preferences, facts, decisions, commitments, or context."
        ),
        param_descriptions={
            "content": "Memory note to record. Keep it concise, grounded, and attributed when needed.",
        },
    )
    async def write_memory(content: str) -> dict:
        """Record a long-term memory note."""
        if not is_enabled:
            return {"status": "disabled", "message": "Memory writing is disabled for this turn."}

        content = content.strip()
        if not content:
            return {"status": "skipped", "message": "Empty content, nothing written."}

        await memory.append_daily(content)
        return {"status": "ok", "message": "Memory recorded."}

    return write_memory


def create_search_memory_tool(
    memory: MemoryStore,
    is_enabled: bool = True,
    message_storage: Optional[MessageStore] = None,
):
    """Create a tool for searching long-term memory by keyword or date range."""

    @function_tool(
        name="search_memory",
        description=(
            "Search older diary memory or raw messages by keyword, date, or date range when recent context is not enough."
        ),
        param_descriptions={
            "query": "Keyword to search. Leave empty for date-only reads.",
            "date": "Date or range: YYYY-MM-DD or YYYY-MM-DD to YYYY-MM-DD.",
            "scope": "Memory area: daily, weekly, monthly, yearly, or all.",
            "context_lines": "Context lines around each match, default 3.",
        },
    )
    async def search_memory(
        query: str = "",
        date: Optional[str] = None,
        scope: str = "all",
        context_lines: int = 3,
    ) -> dict:
        """Search memory files by keyword or date. Returns matching text."""
        if not is_enabled:
            return {"results": "", "enabled": False, "message": "Memory reading is disabled for this turn."}

        context_lines = max(0, min(int(context_lines), 10))
        results = ""

        if query and not date:
            results = await memory.search_keyword(
                query=query,
                scope=scope,
                context_lines=context_lines,
            )
            # Also search raw messages in SQLite
            if message_storage is not None:
                msg_results = await message_storage.search_messages(query=query)
                if msg_results:
                    prefix = "\n\n--- Message Store ---\n" if results else ""
                    results = results + prefix + msg_results
        elif date and not query:
            if " to " in date:
                parts = date.split(" to ", 1)
                results = await memory.search_date_range(
                    start=parts[0].strip(),
                    end=parts[1].strip(),
                )
            else:
                results = await memory.search_date_range(start=date.strip())
        elif query and date:
            # Date-scoped keyword search: read date range, then grep within it
            if " to " in date:
                parts = date.split(" to ", 1)
                date_content = await memory.search_date_range(
                    start=parts[0].strip(),
                    end=parts[1].strip(),
                )
            else:
                date_content = await memory.search_date_range(start=date.strip())
            # Filter lines matching the keyword
            if date_content:
                import re
                pattern = re.compile(re.escape(query), re.IGNORECASE)
                lines = date_content.splitlines()
                matched: list[str] = []
                for i, line in enumerate(lines):
                    if pattern.search(line):
                        start_idx = max(0, i - context_lines)
                        end_idx = min(len(lines), i + context_lines + 1)
                        matched.append("\n".join(lines[start_idx:end_idx]))
                results = "\n---\n".join(matched)
            # Also search raw messages in SQLite with date filter
            if message_storage is not None:
                if " to " in date:
                    parts = date.split(" to ", 1)
                    msg_results = await message_storage.search_messages(
                        query=query,
                        date_start=parts[0].strip(),
                        date_end=parts[1].strip(),
                    )
                else:
                    msg_results = await message_storage.search_messages(
                        query=query,
                        date_start=date.strip(),
                    )
                if msg_results:
                    prefix = "\n\n--- Message Store ---\n" if results else ""
                    results = results + prefix + msg_results
        else:
            # No query and no date — list available files
            files = await memory.list_files(scope=scope)
            results = "\n".join(files)

        return {"results": results, "enabled": True}

    return search_memory
