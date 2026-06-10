"""Dedicated tools for long-term memory access."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from xagent.utils.tool_decorator import function_tool

if TYPE_CHECKING:
    from xagent.components.memory import MarkdownMemory


def create_write_memory_tool(
    memory: MarkdownMemory,
    is_enabled: bool = True,
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
        if not is_enabled:
            return {"status": "disabled", "message": "Memory writing is disabled for this turn."}

        content = content.strip()
        if not content:
            return {"status": "skipped", "message": "Empty content, nothing written."}

        await memory.append_daily(content)
        return {"status": "ok", "message": "Memory recorded."}

    return write_memory


def create_search_memory_tool(
    memory: MarkdownMemory,
    is_enabled: bool = True,
):
    """Create a tool for searching long-term memory by keyword or date range."""

    @function_tool(
        name="search_memory",
        description=(
            "Search long-term memory by keyword or date range. "
            "Use this when the user asks about past conversations, preferences, plans, "
            "or remembered facts that are not in the recent context. "
            "Do not call this on every turn — prefer the recent memory context already "
            "in the system prompt."
        ),
        param_descriptions={
            "query": "Keyword to search for. Leave empty when only searching by date.",
            "date": "A single date (YYYY-MM-DD) or date range (YYYY-MM-DD to YYYY-MM-DD) to read.",
            "scope": "Which time-memory area to search: daily, weekly, monthly, yearly, or all. Default: all.",
            "context_lines": "Number of context lines around each keyword match. Default: 3.",
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
        else:
            # No query and no date — list available files
            files = await memory.list_files(scope=scope)
            results = "\n".join(files)

        return {"results": results, "enabled": True}

    return search_memory
