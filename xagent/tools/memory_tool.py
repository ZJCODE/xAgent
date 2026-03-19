"""Dedicated memory tools for the markdown-based diary system."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Optional

from xagent.utils.tool_decorator import function_tool

if TYPE_CHECKING:
    from xagent.components.memory.markdown_memory import MarkdownMemory
    from xagent.components.memory.helper.llm_service import JournalLLMService


def create_write_daily_memory_tool(
    memory: MarkdownMemory,
    is_enabled,
):
    """Create a tool that appends a diary entry to today's daily markdown file."""

    @function_tool(
        name="write_daily_memory",
        description=(
            "Append a diary entry to today's daily memory file. "
            "Use this when you want to record something worth remembering — "
            "a key decision, preference, commitment, or notable event from the conversation. "
            "You compose the content yourself in natural diary style."
        ),
        param_descriptions={
            "content": "The diary entry text to append. Write in first person, natural diary style.",
        },
    )
    async def write_daily_memory(content: str) -> dict:
        """Append a diary entry to the daily markdown file."""
        if not is_enabled():
            return {"status": "disabled", "message": "Memory is disabled for this turn."}

        content = content.strip()
        if not content:
            return {"status": "skipped", "message": "Empty content, nothing written."}

        path = await memory.append_daily(content)
        return {
            "status": "ok",
            "date": date.today().isoformat(),
            "file": str(path),
        }

    return write_daily_memory


def create_search_memory_tool(
    memory: MarkdownMemory,
    is_enabled,
):
    """Create a tool for searching memory files by keyword or date range."""

    @function_tool(
        name="search_memory",
        description=(
            "Search older diary/memory files by keyword or date range. "
            "Use this when the user asks about past conversations, preferences, plans, "
            "or remembered facts that are not in the recent context. "
            "Do not call this on every turn — prefer the recent diary context already "
            "in the system prompt."
        ),
        param_descriptions={
            "query": "Keyword to search for via grep. Leave empty when only searching by date.",
            "date": "A single date (YYYY-MM-DD) or date range (YYYY-MM-DD to YYYY-MM-DD) to read.",
            "scope": "Which memory directory to search: daily, weekly, monthly, yearly, or all. Default: all.",
            "context_lines": "Number of context lines around each grep match. Default: 3.",
        },
    )
    async def search_memory(
        query: str = "",
        date: Optional[str] = None,
        scope: str = "all",
        context_lines: int = 3,
    ) -> dict:
        """Search memory files by keyword or date. Returns matching text."""
        if not is_enabled():
            return {"results": "", "enabled": False, "message": "Memory is disabled for this turn."}

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
                    scope=scope,
                )
            else:
                results = await memory.search_date_range(start=date.strip(), scope=scope)
        elif query and date:
            # Date-scoped keyword search: read date range, then grep within it
            if " to " in date:
                parts = date.split(" to ", 1)
                date_content = await memory.search_date_range(
                    start=parts[0].strip(),
                    end=parts[1].strip(),
                    scope=scope,
                )
            else:
                date_content = await memory.search_date_range(start=date.strip(), scope=scope)
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


def create_generate_summary_tool(
    memory: MarkdownMemory,
    llm_service: JournalLLMService,
    is_enabled,
):
    """Create a tool for generating weekly/monthly/yearly summaries."""

    @function_tool(
        name="generate_memory_summary",
        description=(
            "Generate a periodic summary (weekly, monthly, or yearly) from daily diary entries. "
            "Use this when the user asks to summarize a period, or when you determine "
            "a completed period needs a summary. Weekly summaries are based on daily entries, "
            "monthly on daily entries, yearly on monthly summaries."
        ),
        param_descriptions={
            "period_type": "Type of summary: 'weekly', 'monthly', or 'yearly'.",
            "target_date": (
                "A date within the target period (YYYY-MM-DD). "
                "For weekly: any date in that week. "
                "For monthly: any date in that month (e.g. 2026-03-01). "
                "For yearly: any date in that year (e.g. 2026-01-01). "
                "Defaults to today if omitted."
            ),
        },
    )
    async def generate_memory_summary(
        period_type: str = "weekly",
        target_date: Optional[str] = None,
    ) -> dict:
        """Generate a summary for the specified period."""
        if not is_enabled():
            return {"status": "disabled", "message": "Memory is disabled for this turn."}

        from datetime import date as date_cls, timedelta

        period_type = period_type.lower()
        if period_type not in ("weekly", "monthly", "yearly"):
            return {"status": "error", "message": f"Invalid period_type: {period_type}"}

        d = date_cls.fromisoformat(target_date) if target_date else date_cls.today()

        if period_type == "weekly":
            week_start, week_end = memory.week_range_for(d)
            source = await memory.search_date_range(
                start=week_start.isoformat(),
                end=week_end.isoformat(),
            )
            label = f"{week_start.isoformat()} to {week_end.isoformat()}"
            out_path = memory.weekly_path(week_start, week_end)

        elif period_type == "monthly":
            import calendar
            first_day = d.replace(day=1)
            last_day = d.replace(day=calendar.monthrange(d.year, d.month)[1])
            source = await memory.search_date_range(
                start=first_day.isoformat(),
                end=last_day.isoformat(),
            )
            label = f"{d.year}-{d.month:02d}"
            out_path = memory.monthly_path(d.year, d.month)

        else:  # yearly
            # For yearly, read monthly summaries
            parts: list[str] = []
            for m in range(1, 13):
                mp = memory.monthly_path(d.year, m)
                text = await memory.read_file(mp)
                if text.strip():
                    parts.append(f"# {d.year}-{m:02d}\n\n{text}")
            source = "\n\n".join(parts)
            label = str(d.year)
            out_path = memory.yearly_path(d.year)

        if not source.strip():
            return {
                "status": "skipped",
                "message": f"No source material found for {period_type} summary ({label}).",
            }

        summary = await llm_service.generate_summary(
            source_content=source,
            period_type=period_type,
            period_label=label,
        )
        if not summary:
            return {"status": "error", "message": "LLM failed to generate summary."}

        await memory.write_summary(out_path, summary)
        return {
            "status": "ok",
            "period_type": period_type,
            "label": label,
            "file": str(out_path),
            "summary": summary,
        }

    return generate_memory_summary
