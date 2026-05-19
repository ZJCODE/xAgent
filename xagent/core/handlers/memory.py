"""Memory handler: recent context injection and background diary writing."""

from __future__ import annotations

import asyncio
import calendar
import logging
import time
from datetime import date, timedelta
from typing import TYPE_CHECKING, List, Optional

from ..config import AgentConfig
from ...components.memory import MemoryKind, SubjectType
from ...schemas import Message, MessageType

if TYPE_CHECKING:
    from ...components.memory import ExperienceMemoryStore, JournalLLMService

logger = logging.getLogger(__name__)


class MemoryHandler:
    """Manages memory context injection and background memory persistence."""

    RECENT_DAYS = AgentConfig.MEMORY_RECENT_DAYS
    MESSAGE_THRESHOLD = AgentConfig.MEMORY_MESSAGE_THRESHOLD
    MIN_INTERVAL_SECONDS = AgentConfig.MEMORY_MIN_INTERVAL_SECONDS
    STALE_FLUSH_SECONDS = AgentConfig.MEMORY_STALE_FLUSH_SECONDS

    def __init__(
        self,
        memory: ExperienceMemoryStore,
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
        self._pending_started_at: Optional[float] = None
        self._last_write_time: float = 0.0
        self._flush_timer_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Context retrieval (injected into system prompt every turn)
    # ------------------------------------------------------------------

    async def get_recent_context(self, days: int | None = None) -> str:
        """Return a compact brief of durable facts plus recent episodic memory.

        The prompt layer should receive reusable memory, not database-shaped rows.
        """
        active_days = self._positive_int(days, self.recent_days)
        today = date.today()
        recent_start = today - timedelta(days=max(active_days - 1, 0))

        durable_result, recent_result = await asyncio.gather(
            self.memory.recall_memory(
                query="",
                max_items=6,
                kinds=[
                    MemoryKind.PREFERENCE,
                    MemoryKind.COMMITMENT,
                    MemoryKind.PROJECT_STATE,
                    MemoryKind.PERSON_FACT,
                    MemoryKind.SEMANTIC_FACT,
                    MemoryKind.PROCEDURE,
                ],
            ),
            self.memory.recall_memory(
                query="",
                max_items=4,
                time_range=(recent_start, today),
                kinds=[MemoryKind.EPISODIC, MemoryKind.SUMMARY],
            ),
        )

        durable_items = durable_result.get("items", [])
        recent_items = recent_result.get("items", [])
        if not durable_items and not recent_items:
            return ""

        sections: list[str] = []
        if durable_items:
            sections.append("Durable facts:")
            for item in durable_items:
                sections.append(self._render_memory_brief_line(item))
        if recent_items:
            if sections:
                sections.append("")
            sections.append(f"Recent episodes ({active_days}d):")
            for item in recent_items:
                sections.append(self._render_memory_brief_line(item, include_time=True))

        return "\n".join(sections).strip()

    # ------------------------------------------------------------------
    # Background diary write
    # ------------------------------------------------------------------

    def schedule_diary_write(self, messages: List[dict]) -> None:
        """Accumulate messages and schedule a background diary write when appropriate.

        Flushes immediately for regular batches when the threshold and write
        interval allow it, and always schedules a stale fallback so short
        conversations cannot sit only in RAM indefinitely.
        """
        if not messages:
            return

        self._pending_messages.extend(messages)

        now = time.time()
        if self._pending_started_at is None:
            self._pending_started_at = now

        threshold_met = len(self._pending_messages) >= self.message_threshold
        interval_met = (now - self._last_write_time) >= self.min_interval_seconds
        stale_met = (now - self._pending_started_at) >= self.stale_flush_seconds

        if stale_met or (threshold_met and interval_met):
            self._flush_diary_write()
            return

        self._schedule_flush_timer(now)

    async def flush_pending(self) -> None:
        """Write pending messages now and wait for in-flight memory tasks."""
        self._cancel_flush_timer()

        if self._pending_messages:
            batch = list(self._pending_messages)
            self._pending_messages.clear()
            self._pending_started_at = None
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
            "metadata": metadata,
            "event_id": metadata.get("event_id"),
            "timestamp": message.timestamp,
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
        self._pending_started_at = None
        self._last_write_time = time.time()

        task = asyncio.create_task(self._do_diary_write(batch))
        self._background_tasks.add(task)
        task.add_done_callback(self._on_task_done)

    async def _do_diary_write(self, messages: List[dict]) -> None:
        """Synthesize experience and store one episodic summary plus durable facts."""
        today_str = date.today().isoformat()
        try:
            synthesis = await self._synthesize_memory(messages, today_str)
            event_ids = [
                int(message["event_id"])
                for message in messages
                if message.get("event_id")
            ]

            if synthesis["experience_summary"].strip():
                await self.memory.remember(
                    content=synthesis["experience_summary"],
                    kind=MemoryKind.EPISODIC,
                    subject_type=SubjectType.SELF,
                    subject_key="self",
                    title=f"Experience on {today_str}",
                    salience=0.55,
                    confidence=0.75,
                    observed_at=date.fromisoformat(today_str),
                    metadata={"source": synthesis["source"], "journal_date": today_str},
                    evidence_event_ids=event_ids,
                    evidence_note=synthesis["experience_summary"][:500],
                    extractor_model=getattr(self.llm_service, "model", None),
                )

            for fact in synthesis["facts"]:
                await self._remember_fact_update(
                    fact,
                    messages=messages,
                    journal_date=today_str,
                )

            if synthesis["experience_summary"].strip() or synthesis["facts"]:
                logger.debug(
                    "Background memory synthesis: %d msgs -> %d chars, %d facts",
                    len(messages),
                    len(synthesis["experience_summary"]),
                    len(synthesis["facts"]),
                )
        except Exception as exc:
            logger.error("Background diary write failed: %s", exc)

    async def _synthesize_memory(self, messages: List[dict], journal_date: str) -> dict:
        synthesizer = getattr(self.llm_service, "synthesize_memory", None)
        if synthesizer is None:
            content = await self.llm_service.format_diary_entry(
                messages=messages,
                journal_date=journal_date,
            )
            facts = await self._extract_legacy_people_facts(messages, content, journal_date)
            return {
                "experience_summary": content,
                "facts": facts,
                "source": "auto_diary",
            }

        synthesis = await synthesizer(messages=messages, journal_date=journal_date)
        if isinstance(synthesis, dict):
            experience_summary = synthesis.get("experience_summary") or ""
            raw_facts = synthesis.get("facts", []) or []
        else:
            experience_summary = getattr(synthesis, "experience_summary", "") or ""
            raw_facts = getattr(synthesis, "facts", []) or []
        facts: list[dict] = []
        for fact in raw_facts:
            if hasattr(fact, "model_dump"):
                facts.append(fact.model_dump())
            elif isinstance(fact, dict):
                facts.append(dict(fact))
        return {
            "experience_summary": str(experience_summary).strip(),
            "facts": facts,
            "source": "memory_synthesis",
        }

    async def _extract_legacy_people_facts(
        self,
        messages: List[dict],
        diary_entry: str,
        journal_date: str,
    ) -> list[dict]:
        return await self._update_people_profiles(
            messages,
            diary_entry,
            journal_date,
            persist=False,
        )

    async def _remember_fact_update(
        self,
        fact: dict,
        *,
        messages: List[dict],
        journal_date: str,
    ) -> None:
        kind = str(fact.get("kind") or "").strip()
        subject_type = str(fact.get("subject_type") or "").strip()
        subject_key = str(fact.get("subject_key") or "").strip()
        content = str(fact.get("content") or "").strip()
        evidence = str(fact.get("evidence") or "").strip()
        if (
            kind not in MemoryKind.VALUES
            or subject_type not in SubjectType.VALUES
            or not subject_key
            or not content
            or not evidence
        ):
            return

        event_ids = [
            int(message["event_id"])
            for message in messages
            if message.get("event_id")
            and (
                subject_type != SubjectType.PERSON
                or str(message.get("sender_id") or "") == subject_key
            )
        ]
        title = str(fact.get("title") or "").strip() or self._default_fact_title(subject_key, content)
        metadata = {"source": fact.get("source") or "memory_synthesis"}
        display_name = str(fact.get("display_name") or "").strip()
        if display_name:
            metadata["display_name"] = display_name

        await self.memory.remember(
            content=content,
            kind=kind,
            subject_type=subject_type,
            subject_key=subject_key,
            title=title,
            salience=self._clamp_score(fact.get("salience"), default=0.7),
            confidence=self._clamp_score(fact.get("confidence"), default=0.85),
            observed_at=date.fromisoformat(journal_date),
            metadata=metadata,
            evidence_event_ids=event_ids,
            evidence_note=evidence,
            extractor_model=getattr(self.llm_service, "model", None),
        )

    async def _update_people_profiles(
        self,
        messages: List[dict],
        diary_entry: str,
        journal_date: str,
        *,
        persist: bool = True,
    ) -> list[dict]:
        extractor = getattr(self.llm_service, "extract_people_profile_updates", None)
        if extractor is None:
            return []

        try:
            profile_updates = await extractor(
                messages=messages,
                diary_entry=diary_entry,
                journal_date=journal_date,
            )
            updates = getattr(profile_updates, "updates", []) or []
            collected: list[dict] = []
            for update in updates:
                update_data = update.model_dump() if hasattr(update, "model_dump") else dict(update)
                person_key = str(update_data.get("person_key") or "").strip()
                fact = str(update_data.get("fact") or "").strip()
                evidence = str(update_data.get("evidence") or "").strip()
                if not person_key or not fact or not evidence:
                    continue
                fact_payload = {
                    "kind": MemoryKind.PERSON_FACT,
                    "subject_type": SubjectType.PERSON,
                    "subject_key": person_key,
                    "title": f"{str(update_data.get('display_name') or person_key).strip()}: {fact[:80]}",
                    "content": fact,
                    "evidence": evidence,
                    "source": update_data.get("source") or "people_profile_extractor",
                    "display_name": update_data.get("display_name") or person_key,
                    "salience": 0.7,
                    "confidence": 0.85,
                }
                collected.append(fact_payload)
                if not persist:
                    continue
                event_ids = [
                    int(message["event_id"])
                    for message in messages
                    if message.get("event_id") and str(message.get("sender_id") or "") == person_key
                ]
                await self.memory.remember(
                    content=fact,
                    kind=MemoryKind.PERSON_FACT,
                    subject_type=SubjectType.PERSON,
                    subject_key=person_key,
                    title=f"{str(update_data.get('display_name') or person_key).strip()}: {fact[:80]}",
                    salience=0.7,
                    confidence=0.85,
                    observed_at=date.fromisoformat(journal_date),
                    metadata={
                        "source": update_data.get("source") or "people_profile_extractor",
                        "display_name": update_data.get("display_name") or person_key,
                    },
                    evidence_event_ids=event_ids,
                    evidence_note=evidence,
                    extractor_model=getattr(self.llm_service, "model", None),
                )
            return collected
        except Exception as exc:
            logger.warning("People profile update skipped: %s", exc)
            return []

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
        first = date(last_year, last_month, 1)
        last = date(last_year, last_month, calendar.monthrange(last_year, last_month)[1])
        if not await self._summary_exists("monthly", first, last):
            await self._generate_monthly(last_year, last_month)

        # Yearly: check last year
        last_year_val = today.year - 1
        year_start = date(last_year_val, 1, 1)
        year_end = date(last_year_val, 12, 31)
        if not await self._summary_exists("yearly", year_start, year_end):
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

        if await self._summary_exists("weekly", week_start, week_end):
            return False

        return await self._generate_weekly(week_start, week_end)

    async def _generate_weekly(self, week_start: date, week_end: date) -> bool:
        source = await self._memory_source_for_range(week_start, week_end)
        if not source.strip():
            return False
        label = f"{week_start.isoformat()} to {week_end.isoformat()}"
        summary = await self.llm_service.generate_summary(source, "weekly", label)
        if summary:
            await self._write_summary("weekly", week_start, week_end, summary)
            logger.info("Generated weekly summary: %s", label)
            return True
        return False

    async def _generate_monthly(self, year: int, month: int) -> None:
        import calendar
        first = date(year, month, 1)
        last = date(year, month, calendar.monthrange(year, month)[1])
        source = await self._memory_source_for_range(first, last)
        if not source.strip():
            return
        label = f"{year}-{month:02d}"
        summary = await self.llm_service.generate_summary(source, "monthly", label)
        if summary:
            await self._write_summary("monthly", first, last, summary)
            logger.info("Generated monthly summary: %s", label)

    async def _generate_yearly(self, year: int) -> None:
        year_start = date(year, 1, 1)
        year_end = date(year, 12, 31)
        source = await self._memory_source_for_range(year_start, year_end)
        if not source.strip():
            return
        label = str(year)
        summary = await self.llm_service.generate_summary(source, "yearly", label)
        if summary:
            await self._write_summary("yearly", year_start, year_end, summary)
            logger.info("Generated yearly summary: %s", label)

    async def _summary_exists(self, summary_type: str, period_start: date, period_end: date) -> bool:
        return await self.memory.summary_exists(
            summary_type=summary_type,
            period_start=period_start,
            period_end=period_end,
            scope_type=SubjectType.SELF,
            scope_key="self",
        )

    async def _memory_source_for_range(self, period_start: date, period_end: date) -> str:
        result = await self.memory.recall_memory(
            query="",
            time_range=(period_start, period_end),
            kinds=[MemoryKind.EPISODIC, MemoryKind.SEMANTIC_FACT, MemoryKind.PREFERENCE, MemoryKind.COMMITMENT],
            max_items=200,
        )
        parts = [
            f"# {item.get('kind')} {item.get('observed_at')}\n\n{item.get('content')}"
            for item in result.get("items", [])
            if str(item.get("content") or "").strip()
        ]
        return "\n\n".join(parts)

    async def _write_summary(self, summary_type: str, period_start: date, period_end: date, content: str) -> None:
        await self.memory.add_summary(
            summary_type=summary_type,
            scope_type=SubjectType.SELF,
            scope_key="self",
            period_start=period_start,
            period_end=period_end,
            content=content,
        )

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
        if not self._pending_messages or self._pending_started_at is None:
            return

        now = now or time.time()
        next_deadline = self._pending_started_at + self.stale_flush_seconds
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
    def _render_memory_brief_line(item: dict, *, include_time: bool = False) -> str:
        subject = item.get("subject", {}) or {}
        subject_type = subject.get("type") or "subject"
        subject_key = subject.get("key") or "unknown"
        parts = [f"[{item.get('kind') or 'memory'}]", f"[{subject_type}:{subject_key}]"]
        if include_time:
            observed_at = item.get("observed_at")
            if observed_at:
                parts.append(f"[{str(observed_at)[:10]}]")
        content = " ".join(str(item.get("content") or "").strip().split())
        return f"- {''.join(parts)} {content}".strip()

    @staticmethod
    def _default_fact_title(subject_key: str, content: str) -> str:
        snippet = " ".join(content.strip().split())[:80]
        return f"{subject_key}: {snippet}" if subject_key else snippet

    @staticmethod
    def _clamp_score(value: object, *, default: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = float(default)
        return min(1.0, max(0.0, parsed))

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
