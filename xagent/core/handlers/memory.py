"""Memory handler: recent context injection and background diary writing."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, timedelta
from typing import TYPE_CHECKING, List, Optional

from ..config import AgentConfig
from ...schemas import Message, MessageType

if TYPE_CHECKING:
    from ...components.memory import JournalLLMService, MarkdownMemory

logger = logging.getLogger(__name__)


class MemoryHandler:
    """Manages diary context injection and background diary persistence.

    Replaces the old ``MemoryManager`` with a simple markdown-file-backed
    implementation.  No SQLite, no FTS — just files + shell commands.
    """

    RECENT_DAYS = AgentConfig.MEMORY_RECENT_DAYS
    MESSAGE_THRESHOLD = AgentConfig.MEMORY_MESSAGE_THRESHOLD
    MIN_INTERVAL_SECONDS = AgentConfig.MEMORY_MIN_INTERVAL_SECONDS
    STALE_FLUSH_SECONDS = AgentConfig.MEMORY_STALE_FLUSH_SECONDS

    def __init__(
        self,
        memory: MarkdownMemory,
        llm_service: JournalLLMService,
        *,
        recent_days: Optional[int] = None,
        message_threshold: Optional[int] = None,
        min_interval_seconds: Optional[float] = None,
        stale_flush_seconds: Optional[float] = None,
    ) -> None:
        self.memory = memory
        self.llm_service = llm_service
        self.recent_days = self._positive_int(recent_days, self.RECENT_DAYS)
        self.message_threshold = self._positive_int(message_threshold, self.MESSAGE_THRESHOLD)
        self.min_interval_seconds = self._non_negative_float(
            min_interval_seconds,
            self.MIN_INTERVAL_SECONDS,
        )
        self.stale_flush_seconds = self._positive_float(
            stale_flush_seconds,
            self.STALE_FLUSH_SECONDS,
        )
        self._background_tasks: set[asyncio.Task] = set()
        self._pending_messages: List[dict] = []
        self._last_activity_time: Optional[float] = None
        self._last_write_time: float = 0.0
        self._flush_timer_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Context retrieval (injected into system prompt every turn)
    # ------------------------------------------------------------------

    async def get_recent_context(self, days: int | None = None) -> str:
        """Read the last *days* daily files and return them as a single string.

        This is injected verbatim into the system prompt so the model always
        has recent diary context without needing a tool call.
        """
        days = days or self.recent_days
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

        Flushes immediately for regular batches when the threshold and write
        interval allow it, and always schedules an idle fallback so short
        conversations cannot sit only in RAM indefinitely.
        """
        if not messages:
            return

        self._pending_messages.extend(messages)

        now = time.time()
        self._last_activity_time = now

        threshold_met = len(self._pending_messages) >= self.message_threshold
        interval_met = (now - self._last_write_time) >= self.min_interval_seconds

        if threshold_met and interval_met:
            self._flush_diary_write()
            return

        self._schedule_flush_timer(now)

    async def flush_pending(self) -> None:
        """Write pending messages now and wait for in-flight memory tasks."""
        self._cancel_flush_timer()

        if self._pending_messages:
            batch = list(self._pending_messages)
            self._pending_messages.clear()
            self._last_activity_time = None
            self._last_write_time = time.time()
            await self._do_diary_write(batch)

        if self._background_tasks:
            tasks = list(self._background_tasks)
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logger.error("Background memory task failed during flush: %s", result)

    def schedule_experience_write(
        self,
        messages: List[Message],
        caused_reply: bool = False,
    ) -> None:
        """Accumulate agent experiences for diary memory.

        The experience stream includes direct conversations and meaningful
        observations. Tool messages remain transient and are intentionally not
        written as diary source material.
        """
        records = [
            self._experience_record(message)
            for message in messages[-AgentConfig.MAX_EXPERIENCE_MEMORY_EVENTS:]
            if self._is_memory_worthy_experience(message, caused_reply=caused_reply)
        ]
        self.schedule_diary_write(records)

    @staticmethod
    def _experience_record(message: Message) -> dict:
        metadata = dict(message.metadata or {})
        return {
            "role": message.role.value,
            "type": message.type.value,
            "sender_id": message.sender_id,
            "content": message.content,
            "timestamp": message.timestamp,
            "metadata": metadata,
        }

    @staticmethod
    def _is_memory_worthy_experience(
        message: Message,
        caused_reply: bool = False,
    ) -> bool:
        if message.type == MessageType.Message:
            return bool(message.content.strip())
        if message.type != MessageType.CONTEXT_EVENT:
            return False

        metadata = message.metadata or {}
        policy = str(metadata.get("memory_policy", "auto")).lower()
        if policy == "never":
            return False
        if policy == "always" or metadata.get("memory_worthy") is True:
            return True
        if caused_reply:
            return True

        event_type = str(metadata.get("event_type", "observation")).lower()
        routine_types = {"heartbeat", "ping", "sensor_tick", "presence_tick"}
        return event_type not in routine_types and bool(message.content.strip())

    def _flush_diary_write(self) -> None:
        """Spawn a background task to format and append pending messages."""
        if not self._pending_messages:
            return

        self._cancel_flush_timer()

        batch = list(self._pending_messages)
        self._pending_messages.clear()
        self._last_activity_time = None
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

        Only generates summaries for periods that are fully in the past and
        whose summary file does not yet exist.
        """
        today = date.today()

        await self.generate_previous_weekly_summary_if_missing(today=today)

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

    async def generate_previous_weekly_summary_if_missing(
        self,
        today: Optional[date] = None,
    ) -> bool:
        """Generate the previous completed week's summary if it is missing."""
        current_day = today or date.today()
        last_week_day = current_day - timedelta(days=7)
        week_start, week_end = self.memory.week_range_for(last_week_day)
        if week_end >= current_day:
            return False

        weekly_path = self.memory.weekly_path(week_start, week_end)
        if weekly_path.exists():
            return False

        return await self._generate_weekly(week_start, week_end)

    async def _generate_weekly(self, week_start: date, week_end: date) -> bool:
        source = await self.memory.search_date_range(
            start=week_start.isoformat(),
            end=week_end.isoformat(),
        )
        if not source.strip():
            return False
        label = f"{week_start.isoformat()} to {week_end.isoformat()}"
        summary = await self.llm_service.generate_summary(source, "weekly", label)
        if summary:
            await self.memory.write_summary(self.memory.weekly_path(week_start, week_end), summary)
            logger.info("Generated weekly summary: %s", label)
            return True
        return False

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

    def _schedule_flush_timer(self, now: Optional[float] = None) -> None:
        if not self._pending_messages or self._last_activity_time is None:
            return

        now = now or time.time()
        next_deadline = self._last_activity_time + self.stale_flush_seconds
        if len(self._pending_messages) >= self.message_threshold:
            next_deadline = min(next_deadline, self._last_write_time + self.min_interval_seconds)
        delay = max(0.0, next_deadline - now)

        self._cancel_flush_timer()
        task = asyncio.create_task(self._run_flush_timer(delay))
        self._flush_timer_task = task
        task.add_done_callback(self._on_flush_timer_done)

    async def _run_flush_timer(self, delay: float) -> None:
        await asyncio.sleep(delay)
        self._flush_timer_task = None
        self._flush_diary_write()

    def _on_flush_timer_done(self, task: asyncio.Task) -> None:
        if self._flush_timer_task is task:
            self._flush_timer_task = None
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            logger.error("Memory flush timer failed: %s", exc)

    def _cancel_flush_timer(self) -> None:
        task = self._flush_timer_task
        if task is None:
            return
        try:
            current_task = asyncio.current_task()
        except RuntimeError:
            current_task = None
        if task is not current_task and not task.done():
            task.cancel()
        if task is not current_task:
            self._flush_timer_task = None

    @staticmethod
    def _positive_int(value: Optional[int], default: int) -> int:
        if value is None:
            return default
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    @staticmethod
    def _positive_float(value: Optional[float], default: float) -> float:
        if value is None:
            return float(default)
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return float(default)
        return parsed if parsed > 0 else float(default)

    @staticmethod
    def _non_negative_float(value: Optional[float], default: float) -> float:
        if value is None:
            return float(default)
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return float(default)
        return parsed if parsed >= 0 else float(default)
