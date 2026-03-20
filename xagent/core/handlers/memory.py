"""Memory handler: recent context injection and background diary writing."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, timedelta
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from ...components.memory.markdown_memory import MarkdownMemory
    from ...components.memory.helper.llm_service import JournalLLMService

logger = logging.getLogger(__name__)


class MemoryHandler:
    """Manages diary context injection and background diary persistence.

    Replaces the old ``MemoryManager`` with a simple markdown-file-backed
    implementation.  No SQLite, no FTS — just files + shell commands.
    """

    RECENT_DAYS = 3
    MESSAGE_THRESHOLD = 10
    MIN_INTERVAL_SECONDS = 300

    def __init__(
        self,
        memory: MarkdownMemory,
        llm_service: JournalLLMService,
    ) -> None:
        self.memory = memory
        self.llm_service = llm_service
        self._background_tasks: set[asyncio.Task] = set()
        self._pending_messages: List[dict] = []
        self._last_write_time: float = 0.0

    # ------------------------------------------------------------------
    # Context retrieval (injected into system prompt every turn)
    # ------------------------------------------------------------------

    async def get_recent_context(self, days: int | None = None) -> str:
        """Read the last *days* daily files and return them as a single string.

        This is injected verbatim into the system prompt so the model always
        has recent diary context without needing a tool call.
        """
        days = days or self.RECENT_DAYS
        entries = await self.memory.read_recent_dailies(days=days)
        if not entries:
            return ""

        sections: list[str] = []
        for date_str, content in entries:
            sections.append(f"[{date_str}]\n{content.strip()}")
        return "\n\n".join(sections)

    # ------------------------------------------------------------------
    # Background diary write
    # ------------------------------------------------------------------

    def schedule_diary_write(self, messages: List[dict]) -> None:
        """Accumulate messages and schedule a background diary write when appropriate.

        Waits until ``MESSAGE_THRESHOLD`` is reached **and**
        ``MIN_INTERVAL_SECONDS`` have elapsed since the last write.
        """
        if not messages:
            return

        self._pending_messages.extend(messages)

        now = time.time()
        threshold_met = len(self._pending_messages) >= self.MESSAGE_THRESHOLD
        interval_met = (now - self._last_write_time) >= self.MIN_INTERVAL_SECONDS

        if threshold_met and interval_met:
            self._flush_diary_write()

    def _flush_diary_write(self) -> None:
        """Spawn a background task to format and append pending messages."""
        if not self._pending_messages:
            return

        batch = list(self._pending_messages)
        self._pending_messages.clear()
        self._last_write_time = time.time()

        task = asyncio.create_task(self._do_diary_write(batch))
        self._background_tasks.add(task)
        task.add_done_callback(self._on_task_done)

    async def _do_diary_write(self, messages: List[dict]) -> None:
        """LLM-format messages and append to today's daily file."""
        today_str = date.today().isoformat()
        try:
            content = await self.llm_service.format_diary_entry(
                messages=messages,
                journal_date=today_str,
            )
            if content.strip():
                await self.memory.append_daily(content)
                logger.debug("Background diary write: %d msgs → %d chars", len(messages), len(content))
        except Exception as exc:
            logger.error("Background diary write failed: %s", exc)

    # ------------------------------------------------------------------
    # Summary auto-generation
    # ------------------------------------------------------------------

    async def check_and_generate_summaries(self) -> None:
        """Check if any completed periods need summary generation.

        Called in the background after each chat turn.  Only generates
        summaries for periods that are fully in the past and whose summary
        file does not yet exist.
        """
        today = date.today()

        # Weekly: check last week
        last_week_day = today - timedelta(days=7)
        week_start, week_end = self.memory.week_range_for(last_week_day)
        if week_end < today:
            wp = self.memory.weekly_path(week_start, week_end)
            if not wp.exists():
                await self._generate_weekly(week_start, week_end)

        # Monthly: check last month
        if today.month == 1:
            last_month, last_year = 12, today.year - 1
        else:
            last_month, last_year = today.month - 1, today.year
        mp = self.memory.monthly_path(last_year, last_month)
        if not mp.exists():
            await self._generate_monthly(last_year, last_month)

        # Yearly: check last year
        last_year_val = today.year - 1
        yp = self.memory.yearly_path(last_year_val)
        if not yp.exists():
            await self._generate_yearly(last_year_val)

    async def _generate_weekly(self, week_start: date, week_end: date) -> None:
        source = await self.memory.search_date_range(
            start=week_start.isoformat(),
            end=week_end.isoformat(),
        )
        if not source.strip():
            return
        label = f"{week_start.isoformat()} to {week_end.isoformat()}"
        summary = await self.llm_service.generate_summary(source, "weekly", label)
        if summary:
            await self.memory.write_summary(self.memory.weekly_path(week_start, week_end), summary)
            logger.info("Generated weekly summary: %s", label)

    async def _generate_monthly(self, year: int, month: int) -> None:
        import calendar
        first = date(year, month, 1)
        last = date(year, month, calendar.monthrange(year, month)[1])
        source = await self.memory.search_date_range(
            start=first.isoformat(),
            end=last.isoformat(),
        )
        if not source.strip():
            return
        label = f"{year}-{month:02d}"
        summary = await self.llm_service.generate_summary(source, "monthly", label)
        if summary:
            await self.memory.write_summary(self.memory.monthly_path(year, month), summary)
            logger.info("Generated monthly summary: %s", label)

    async def _generate_yearly(self, year: int) -> None:
        parts: list[str] = []
        for m in range(1, 13):
            text = await self.memory.read_file(self.memory.monthly_path(year, m))
            if text.strip():
                parts.append(f"# {year}-{m:02d}\n\n{text}")
        source = "\n\n".join(parts)
        if not source.strip():
            return
        label = str(year)
        summary = await self.llm_service.generate_summary(source, "yearly", label)
        if summary:
            await self.memory.write_summary(self.memory.yearly_path(year), summary)
            logger.info("Generated yearly summary: %s", label)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _on_task_done(self, task: asyncio.Task) -> None:
        self._background_tasks.discard(task)
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            logger.error("Background memory task failed: %s", exc)
