"""Dedicated tools for long-term memory access."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from xagent.utils.search_terms import normalize_terms, score_text
from xagent.utils.tool_decorator import function_tool

if TYPE_CHECKING:
    from xagent.components.memory import MarkdownMemory
    from xagent.components.message import MessageStorage


def create_write_memory_tool(
    memory: MarkdownMemory,
    is_enabled: bool = True,
):
    """Create a tool that records long-term useful memory."""

    @function_tool(
        name="write_memory",
        description=(
            "Record a concise, attributable diary-memory note for durable preferences, "
            "facts, decisions, commitments, or context. Skip trivial or temporary notes."
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
    memory: MarkdownMemory,
    is_enabled: bool = True,
    message_storage: Optional[MessageStorage] = None,
):
    """Create a tool for searching long-term memory by terms or date range."""

    @function_tool(
        name="search_memory",
        description=(
            "Search older diary memory or raw messages by verbatim terms, date, or date range. "
            "Pass concrete words or short phrases likely to appear in memory; results OR-match "
            "terms and rank by how many terms hit. Prefer recent memory already in context when "
            "present; search when older continuity or facts are needed."
        ),
        param_descriptions={
            "query": (
                "List of verbatim search terms or short phrases (e.g. [\"Jun\", \"摄影\"] or "
                "[\"三体\"]). Avoid synonym bags like [\"喜欢\", \"爱好\", \"兴趣\"]. "
                "Leave empty for date-only reads."
            ),
            "date": "Date or range: YYYY-MM-DD or YYYY-MM-DD to YYYY-MM-DD.",
            "scope": "Memory area: daily, weekly, monthly, yearly, or all.",
            "context_lines": "Context lines around each match, default 3.",
        },
    )
    async def search_memory(
        query: Optional[list[str]] = None,
        date: Optional[str] = None,
        scope: str = "all",
        context_lines: int = 3,
    ) -> dict:
        """Search memory files by terms or date. Returns matching text."""
        if not is_enabled:
            return {"results": "", "enabled": False, "message": "Memory reading is disabled for this turn."}

        terms = normalize_terms(query)
        context_lines = max(0, min(int(context_lines), 10))
        results = ""

        if terms and not date:
            results = await memory.search_keyword(
                terms=terms,
                scope=scope,
                context_lines=context_lines,
            )
            if message_storage is not None:
                msg_results = await message_storage.search_messages(terms=terms)
                if msg_results:
                    prefix = "\n\n--- Message Store ---\n" if results else ""
                    results = results + prefix + msg_results
        elif date and not terms:
            if " to " in date:
                parts = date.split(" to ", 1)
                results = await memory.search_date_range(
                    start=parts[0].strip(),
                    end=parts[1].strip(),
                )
            else:
                results = await memory.search_date_range(start=date.strip())
        elif terms and date:
            if " to " in date:
                parts = date.split(" to ", 1)
                date_content = await memory.search_date_range(
                    start=parts[0].strip(),
                    end=parts[1].strip(),
                )
            else:
                date_content = await memory.search_date_range(start=date.strip())
            if date_content:
                lines = date_content.splitlines()
                candidates: list[tuple[int, int, int, str]] = []
                for i, line in enumerate(lines):
                    if score_text(line, terms) <= 0:
                        continue
                    start_idx = max(0, i - context_lines)
                    end_idx = min(len(lines), i + context_lines + 1)
                    block = "\n".join(lines[start_idx:end_idx])
                    candidates.append((score_text(block, terms), start_idx, end_idx, block))
                candidates.sort(key=lambda item: (-item[0], -item[1]))
                covered: set[int] = set()
                matched_blocks: list[str] = []
                for _, start_idx, end_idx, block in candidates:
                    if any(idx in covered for idx in range(start_idx, end_idx)):
                        continue
                    covered.update(range(start_idx, end_idx))
                    matched_blocks.append(block)
                results = "\n---\n".join(matched_blocks)
            if message_storage is not None:
                if " to " in date:
                    parts = date.split(" to ", 1)
                    msg_results = await message_storage.search_messages(
                        terms=terms,
                        date_start=parts[0].strip(),
                        date_end=parts[1].strip(),
                    )
                else:
                    msg_results = await message_storage.search_messages(
                        terms=terms,
                        date_start=date.strip(),
                    )
                if msg_results:
                    prefix = "\n\n--- Message Store ---\n" if results else ""
                    results = results + prefix + msg_results
        else:
            files = await memory.list_files(scope=scope)
            results = "\n".join(files)

        return {"results": results, "enabled": True}

    return search_memory
