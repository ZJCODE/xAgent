"""Memory handler: recent context injection and count-based diary writing."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import IO, TYPE_CHECKING, List, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX platforms
    msvcrt = None

from ..config import AgentConfig
from ...schemas import Message, MessageType

if TYPE_CHECKING:
    from ...components.memory import JournalLLMService, MarkdownMemory
    from ...components.message import MessageStorageBase

logger = logging.getLogger(__name__)


class MemoryHandler:
    """Manages recent diary context and count-based journal maintenance."""

    RECENT_DAYS = AgentConfig.MEMORY_RECENT_DAYS
    DEFAULT_JOURNAL_SOURCE_CHARS = 24000  # Soft per-batch source budget; records remain intact.

    def __init__(
        self,
        memory: MarkdownMemory,
        llm_service: JournalLLMService,
        message_storage: MessageStorageBase,
        *,
        max_history: int,
        recent_days: Optional[int] = None,
        window_overlap: Optional[int] = None,
        max_journal_source_chars: Optional[int] = None,
    ) -> None:
        self.memory = memory
        self.llm_service = llm_service
        self.message_storage = message_storage
        self.max_history = self._positive_int(max_history, AgentConfig.DEFAULT_MAX_HISTORY)
        self.recent_days = self._positive_int(recent_days, self.RECENT_DAYS)
        if window_overlap is not None:
            self.window_overlap = self._positive_int(window_overlap, 1)
        else:
            self.window_overlap = max(1, int(self.max_history * AgentConfig.MEMORY_WINDOW_OVERLAP_RATIO))
        self.window_overlap = min(self.window_overlap, max(0, self.max_history - 1))
        self.max_journal_source_chars = self._positive_int(
            max_journal_source_chars,
            self.DEFAULT_JOURNAL_SOURCE_CHARS,
        )
        self._maintenance_lock = asyncio.Lock()
        self._maintenance_task: Optional[asyncio.Task[bool]] = None
        state = self._read_state_sync()
        self._last_processed_message_id = self._non_negative_int(
            state.get("last_processed_message_id"),
            0,
        )

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
    # Journal maintenance
    # ------------------------------------------------------------------

    def schedule_experience_write(
        self,
        messages: List[Message],
    ) -> None:
        """Schedule a journal maintenance check after meaningful new experience."""
        if not messages:
            return
        if any(self._is_memory_worthy_experience(message) for message in messages):
            self._schedule_maintenance()

    async def run_maintenance(self, force: bool = False) -> bool:
        """Run journal maintenance using persisted message history only."""
        current_task = asyncio.current_task()
        maintenance_task = self._maintenance_task
        if maintenance_task is not None and maintenance_task is not current_task and not maintenance_task.done():
            try:
                existing_result = await maintenance_task
            except Exception as exc:
                logger.error("Background memory maintenance failed: %s", exc)
                existing_result = False
            if not force:
                return bool(existing_result)

        async with self._maintenance_guard(refresh_state=True):
            return await self._run_maintenance_locked(force=force)

    async def _run_maintenance_locked(self, force: bool = False) -> bool:
        latest_message_id = await self.message_storage.get_latest_message_cursor()
        if latest_message_id <= 0:
            return False

        # Gate on unprocessed message count: only run when enough new
        # messages have accumulated since the last checkpoint.  Using
        # max_history - window_overlap as the effective threshold preserves
        # the same batch overlap that the old per-process interaction
        # counter provided, but works correctly across multiple processes.
        if not force:
            unprocessed_count = latest_message_id - self._last_processed_message_id
            if unprocessed_count < self.max_history - self.window_overlap:
                return False

        # Read the last max_history messages for compression, ensuring
        # window_overlap entries naturally overlap with the previous batch.
        recent_messages = await self.message_storage.get_messages(
            count=self.max_history,
        )
        if not recent_messages:
            return False

        new_records = [
            self._experience_record(message)
            for message in recent_messages
            if self._is_memory_worthy_experience(message)
        ]
        if not new_records:
            await self._commit_processed_message_id(latest_message_id)
            return False

        batches = self._split_records_for_source_budget(new_records)
        if not batches:
            return False

        for batch in batches:
            if not await self._write_journal_entry(batch):
                return False

        if not await self._commit_processed_message_id(latest_message_id):
            logger.warning(
                "Diary write completed but checkpoint was not advanced; retry will replay pending messages."
            )
            return False

        return True

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
    def _is_memory_worthy_experience(message: Message) -> bool:
        if message.type == MessageType.MESSAGE:
            return bool(message.content.strip())
        if message.type != MessageType.CONTEXT_EVENT:
            return False

        metadata = message.metadata or {}
        policy = str(metadata.get("memory_policy", "auto")).lower()
        if policy == "never":
            return False
        if policy == "always" or metadata.get("memory_worthy") is True:
            return True

        event_type = str(metadata.get("event_type", "observation")).lower()
        routine_types = {"heartbeat", "ping", "sensor_tick", "presence_tick"}
        return event_type not in routine_types and bool(message.content.strip())

    async def _write_journal_entry(self, messages: List[dict]) -> bool:
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
                return True
        except Exception as exc:
            logger.error("Background diary write failed: %s", exc)
        return False

    def _split_records_for_source_budget(self, records: List[dict]) -> List[List[dict]]:
        batches: list[list[dict]] = []
        current_batch: list[dict] = []
        current_chars = 0

        for record in records:
            estimated_chars = self._estimate_record_chars(record)
            if current_batch and current_chars + estimated_chars > self.max_journal_source_chars:
                batches.append(current_batch)
                current_batch = [record]
                current_chars = estimated_chars
                continue
            current_batch.append(record)
            current_chars += estimated_chars

        if current_batch:
            batches.append(current_batch)
        return batches

    @staticmethod
    def _estimate_record_chars(record: dict) -> int:
        # Estimate: content length + header overhead (~120 chars for speaker/timestamp markers).
        return len(str(record.get("content", ""))) + 120

    # ------------------------------------------------------------------
    # Summary auto-generation
    # ------------------------------------------------------------------

    async def check_and_generate_summaries(self) -> None:
        """Check if any completed periods need summary generation.

        Only generates summaries for periods that are fully in the past and
        whose summary file does not yet exist.
        """
        async with self._maintenance_guard():
            await self._check_and_generate_summaries_locked()

    async def _check_and_generate_summaries_locked(self) -> None:
        today = date.today()

        await self._generate_previous_weekly_summary_if_missing_locked(today=today)

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
        async with self._maintenance_guard():
            return await self._generate_previous_weekly_summary_if_missing_locked(today=today)

    async def _generate_previous_weekly_summary_if_missing_locked(
        self,
        today: Optional[date] = None,
    ) -> bool:
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
        for month in range(1, 13):
            text = await self.memory.read_file(self.memory.monthly_path(year, month))
            if text.strip():
                parts.append(f"# {year}-{month:02d}\n\n{text}")
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

    def _on_maintenance_done(self, task: asyncio.Task[bool]) -> None:
        if self._maintenance_task is task:
            self._maintenance_task = None
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error("Background memory maintenance failed: %s", exc)

    def _schedule_maintenance(self) -> None:
        task = self._maintenance_task
        if task is not None and not task.done():
            return

        maintenance_task = asyncio.create_task(self.run_maintenance())
        self._maintenance_task = maintenance_task
        maintenance_task.add_done_callback(self._on_maintenance_done)

    @asynccontextmanager
    async def _maintenance_guard(self, refresh_state: bool = False):
        async with self._maintenance_lock:
            async with self._maintenance_process_lock():
                if refresh_state:
                    await self._refresh_state_from_disk()
                yield

    @asynccontextmanager
    async def _maintenance_process_lock(self):
        lock_handle = await asyncio.to_thread(self._acquire_process_lock_sync)
        try:
            yield
        finally:
            await asyncio.to_thread(self._release_process_lock_sync, lock_handle)

    async def _refresh_state_from_disk(self) -> None:
        state = await asyncio.to_thread(self._read_state_sync)
        if "last_processed_message_id" in state:
            self._last_processed_message_id = self._non_negative_int(
                state.get("last_processed_message_id"),
                0,
            )
            return

        legacy_count = self._non_negative_int(state.get("last_processed_message_count"), 0)
        if legacy_count <= 0:
            self._last_processed_message_id = 0
            return

        try:
            migrated_cursor = await self.message_storage.cursor_for_message_count(legacy_count)
        except Exception as exc:
            logger.warning("Failed to migrate legacy journal checkpoint: %s", exc)
            migrated_cursor = 0
        if migrated_cursor <= 0:
            # Legacy count exceeds current row count (stream shrunk) —
            # fall back to latest cursor instead of 0 to avoid reprocessing all.
            try:
                migrated_cursor = await self.message_storage.get_latest_message_cursor()
            except Exception:
                migrated_cursor = 0
        self._last_processed_message_id = self._non_negative_int(migrated_cursor, 0)

    async def _commit_processed_message_id(self, processed_message_id: int) -> bool:
        normalized_id = self._non_negative_int(processed_message_id, 0)
        try:
            await self._write_state(normalized_id)
        except Exception as exc:
            logger.error("Failed to persist journal state: %s", exc)
            return False
        self._last_processed_message_id = normalized_id
        return True

    async def _write_state(self, processed_message_id: int) -> None:
        payload = {
            "last_processed_message_id": self._non_negative_int(processed_message_id, 0),
        }
        await asyncio.to_thread(self._write_state_sync, payload)

    def _read_state_sync(self) -> dict:
        path = self._state_path()
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            logger.warning("Failed to read journal state: %s", exc)
            return {}

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("Failed to decode journal state: %s", exc)
            return {}
        return data if isinstance(data, dict) else {}

    def _write_state_sync(self, payload: dict) -> None:
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.tmp")
        temp_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temp_path.replace(path)

    def _acquire_process_lock_sync(self) -> IO[str]:
        path = self._lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = path.open("a+", encoding="utf-8")
        try:
            self._lock_file(lock_file)
        except Exception:
            lock_file.close()
            raise
        return lock_file

    @staticmethod
    def _lock_file(lock_file: IO[str]) -> None:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            return
        if msvcrt is not None:
            lock_file.seek(0)
            if not lock_file.read(1):
                lock_file.write("\0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            return
        raise RuntimeError("No supported file locking implementation is available")

    def _release_process_lock_sync(self, lock_file: IO[str]) -> None:
        try:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            lock_file.close()

    def _state_path(self) -> Path:
        return self.memory.root / "journal_state.json"

    def _lock_path(self) -> Path:
        return self.memory.root / ".journal_maintenance.lock"

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
    def _non_negative_int(value: Optional[int], default: int) -> int:
        if value is None:
            return int(default)
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return int(default)
        return parsed if parsed >= 0 else int(default)
