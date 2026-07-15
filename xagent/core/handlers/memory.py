"""Memory handler: recent context injection and count-based diary writing."""

from __future__ import annotations

import asyncio
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
from ...schemas import Message, MessageType, RoleType

if TYPE_CHECKING:
    from ...components.memory import MarkdownMemory, RelationshipStore
    from ...components.message import MessageStorage
    from ..journal import JournalLLMService

logger = logging.getLogger(__name__)


class MemoryHandler:
    """Manages recent diary context and count-based journal maintenance."""

    RECENT_DAYS = AgentConfig.MEMORY_RECENT_DAYS
    DEFAULT_JOURNAL_SOURCE_CHARS = 24000  # Soft per-batch source budget; records remain intact.
    SUBCONSCIOUS_SUMMARY_SCOPES = ("yearly", "monthly", "weekly")
    SUBCONSCIOUS_SUMMARY_CHARS_PER_SCOPE = 2000

    def __init__(
        self,
        memory: MarkdownMemory,
        llm_service: JournalLLMService,
        message_storage: MessageStorage,
        *,
        max_history: int,
        recent_days: Optional[int] = None,
        max_journal_source_chars: Optional[int] = None,
        relationship_store: Optional["RelationshipStore"] = None,
    ) -> None:
        self.memory = memory
        self.llm_service = llm_service
        self.message_storage = message_storage
        self.relationship_store = relationship_store
        self.max_history = self._positive_int(max_history, AgentConfig.DEFAULT_MAX_HISTORY)
        self.recent_days = self._non_negative_int(recent_days, self.RECENT_DAYS)
        self.window_overlap = min(
            max(1, int(self.max_history * AgentConfig.MEMORY_WINDOW_OVERLAP_RATIO)),
            self.max_history - 1,
        )
        self.max_journal_source_chars = self._positive_int(
            max_journal_source_chars,
            self.DEFAULT_JOURNAL_SOURCE_CHARS,
        )
        self._maintenance_lock = asyncio.Lock()
        self._maintenance_task: Optional[asyncio.Task[bool]] = None
        self._last_processed_message_id = self._non_negative_int(
            self._read_state_sync(),
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
        days = self.recent_days if days is None else self._non_negative_int(days, self.recent_days)
        entries = await self.memory.read_recent_dailies(days=days)
        if not entries:
            return ""

        sections: list[str] = []
        for date_str, content in entries:
            sections.append(f"[{date_str}]\n{content.strip()}")
        return "\n\n".join(sections)

    async def get_subconscious_context(self, days: int | None = None) -> str:
        """Return memory context for subconscious turns.

        Subconscious thinking should stay grounded in the same diary stream as
        normal turns, but it benefits from a slightly wider time horizon than
        the recent daily window.
        """
        sections: list[str] = []

        summary_sections = await self._latest_summary_sections_for_subconscious()
        if summary_sections:
            sections.append("Longer-range diary summaries:\n" + "\n\n".join(summary_sections))

        recent = await self.get_recent_context(days=days)
        if recent.strip():
            sections.append("Recent daily diary:\n" + recent.strip())

        return "\n\n".join(sections)

    async def _latest_summary_sections_for_subconscious(self) -> list[str]:
        sections: list[str] = []
        for scope in self.SUBCONSCIOUS_SUMMARY_SCOPES:
            try:
                files = await self.memory.list_files(scope=scope)
            except Exception as exc:
                logger.warning("Failed to list %s memory summaries: %s", scope, exc, exc_info=True)
                continue

            for file_name in reversed(files):
                path = Path(file_name)
                try:
                    content = await self.memory.read_file(path)
                except Exception as exc:
                    logger.warning("Failed to read %s memory summary: %s", scope, exc, exc_info=True)
                    continue
                text = content.strip()
                if not text:
                    continue
                label = path.stem
                trimmed = self._trim_subconscious_summary(text)
                sections.append(f"[{scope}: {label}]\n{trimmed}")
                break
        return sections

    @classmethod
    def _trim_subconscious_summary(cls, text: str) -> str:
        limit = max(1, int(cls.SUBCONSCIOUS_SUMMARY_CHARS_PER_SCOPE))
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + "\n[summary truncated]"

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

    async def run_maintenance(
        self, force: bool = False, trigger: str = "count", idle_seconds: float = 0
    ) -> bool:
        """Run journal maintenance using persisted message history only.

        Args:
            force: When True, skip the unprocessed-message gate.
            trigger: ``"count"`` when triggered by accumulating enough messages;
                     ``"idle"`` when triggered by idle timeout.
            idle_seconds: Seconds since last interaction (only meaningful when
                          *trigger* is ``"idle"``).
        """
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
            return await self._run_maintenance_locked(
                force=force, trigger=trigger, idle_seconds=idle_seconds
            )

    async def _run_maintenance_locked(
        self, force: bool = False, trigger: str = "count", idle_seconds: float = 0
    ) -> bool:
        latest_message_id = await self.message_storage.get_latest_message_cursor()
        if latest_message_id <= 0:
            return False
        if latest_message_id <= self._last_processed_message_id:
            return False

        # Gate on unprocessed message count: only run when enough new
        # messages have accumulated since the last checkpoint.  Based on
        # the persisted cursor so it is safe across multiple processes.
        if not force:
            unprocessed_count = latest_message_id - self._last_processed_message_id
            if unprocessed_count < self.max_history - self.window_overlap:
                return False

        # Build a cursor-bounded window that starts window_overlap entries
        # before the last checkpoint (for diary continuity between adjacent
        # batches) and is capped at max_history (to bound the LLM budget
        # even when many messages accumulate between maintenance cycles).
        # Advancing the checkpoint to the batch end rather than to
        # latest_message_id ensures overflow messages are not dropped.
        start_exclusive = max(0, self._last_processed_message_id - self.window_overlap)
        end_inclusive = min(latest_message_id, start_exclusive + self.max_history)
        if end_inclusive <= 0:
            return False

        recent_messages = await self.message_storage.get_messages_in_cursor_range(
            start_exclusive=start_exclusive,
            end_inclusive=end_inclusive,
        )
        if not recent_messages:
            # Jump checkpoint forward.  If messages were deleted (id gap),
            # leap to just before latest so the next cycle catches real data
            # instead of inching forward one window at a time.
            jump_to = max(end_inclusive, latest_message_id - self.max_history)
            await self._commit_processed_message_id(jump_to)
            return False

        new_records = [
            self._experience_record(message)
            for message in recent_messages
            if self._is_memory_worthy_experience(message)
        ]
        if not new_records:
            await self._commit_processed_message_id(end_inclusive)
            return False

        batches = self._split_records_for_source_budget(new_records)
        if not batches:
            return False

        for batch in batches:
            if not await self._write_journal_entry(
                batch,
                trigger=trigger,
                start_cursor=start_exclusive,
                end_cursor=end_inclusive,
                idle_seconds=idle_seconds,
            ):
                return False

        await self._update_relationship_cards(recent_messages, new_records)

        if not await self._commit_processed_message_id(end_inclusive):
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

    # ------------------------------------------------------------------
    # Relationship cards (derived projection over the diary)
    # ------------------------------------------------------------------

    async def _update_relationship_cards(
        self,
        recent_messages: List[Message],
        new_records: List[dict],
    ) -> None:
        """Derive/update per-person relationship cards from this batch.

        A best-effort projection over the diary stream: failures here must
        never break diary maintenance, so everything is wrapped defensively.
        """
        if self.relationship_store is None:
            return
        try:
            participants = self._extract_participants(recent_messages)
            if not participants:
                return

            store = self.relationship_store
            existing_cards: dict[str, str] = {}
            for participant in participants:
                card = await store.read_card(participant["key"])
                if card is not None and not card.is_empty:
                    existing_cards[participant["key"]] = card.body

            new_cards = await self.llm_service.update_relationship_cards(
                participants=participants,
                messages=new_records,
                existing_cards=existing_cards,
            )
            if not new_cards:
                logger.info(
                    "Relationship card update: no changes for %d participant(s) — %s",
                    len(participants),
                    ", ".join(p["key"] for p in participants),
                )
                return

            from ...components.memory import RelationshipCard

            today_str = date.today().isoformat()
            participant_by_key = {p["key"]: p for p in participants}
            for key, body in new_cards.items():
                participant = participant_by_key.get(key, {})
                await store.write_card(
                    RelationshipCard(
                        key=key,
                        body=body,
                        display_name=str(participant.get("display_name") or ""),
                        channel=str(participant.get("channel") or ""),
                        user_id=str(participant.get("user_id") or ""),
                        updated=today_str,
                    )
                )
            logger.info(
                "Updated %d relationship card(s): %s",
                len(new_cards),
                ", ".join(f"{k} ({len(v)} chars)" for k, v in new_cards.items()),
            )
        except Exception as exc:
            logger.warning("Relationship card update failed: %s", exc, exc_info=True)

    @staticmethod
    def _extract_participants(messages: List[Message]) -> List[dict]:
        """Collect distinct human participants (non-self) from a batch."""
        from ...components.memory import RelationshipStore

        participants: dict[str, dict] = {}
        for message in messages:
            if message.type != MessageType.MESSAGE:
                continue
            if message.role != RoleType.USER:
                continue
            user_id = (message.sender_id or "").strip()
            if not user_id:
                continue
            channel = (message.channel or "").strip()
            key = RelationshipStore.make_key(channel, user_id)
            if key in participants:
                continue
            metadata = message.metadata or {}
            display_name = str(metadata.get("sender_name") or "").strip() or user_id
            participants[key] = {
                "key": key,
                "display_name": display_name,
                "channel": channel,
                "user_id": user_id,
            }
        return list(participants.values())

    async def get_relationship_context(
        self,
        speaker_keys: List[str],
        participant_keys: Optional[List[str]] = None,
        max_cards: Optional[int] = None,
        include_routing_id: bool = False,
    ) -> str:
        """Return rendered relationship cards for the given people.

        ``speaker_keys`` (the current speaker) are always included first;
        ``participant_keys`` (other people in the room) fill remaining budget.
        ``include_routing_id`` appends each person's ``user_id`` to the header
        so the subconscious can emit a deterministic ``recipient_hint``; reply
        turns leave it off so the identifier is never exposed to users.
        """
        if self.relationship_store is None:
            return ""

        ordered_keys: list[str] = []
        seen: set[str] = set()
        for key in [*(speaker_keys or []), *(participant_keys or [])]:
            normalized = (key or "").strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                ordered_keys.append(normalized)

        resolved_max = max_cards if max_cards is not None else AgentConfig.RELATIONSHIP_MAX_CARDS_PER_TURN
        resolved_max = max(1, resolved_max)
        ordered_keys = ordered_keys[:resolved_max]
        if not ordered_keys:
            return ""

        try:
            cards = await self.relationship_store.read_cards(ordered_keys)
        except Exception as exc:
            logger.warning("Failed to read relationship cards: %s", exc, exc_info=True)
            return ""

        if not cards:
            return ""

        blocks: list[str] = []
        for card in cards:
            name = card.display_name or card.user_id or card.key
            body = card.body.strip()
            if include_routing_id and card.user_id:
                header = f"## {name} [user_id: {card.user_id}]"
            else:
                header = f"## {name}"
            blocks.append(f"{header}\n{body}")
        return "\n\n".join(blocks)

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

    async def _write_journal_entry(
        self,
        messages: List[dict],
        trigger: str = "count",
        start_cursor: int = 0,
        end_cursor: int = 0,
        idle_seconds: float = 0,
    ) -> bool:
        """LLM-format messages and append to today's daily file."""
        today_str = date.today().isoformat()

        try:
            content = await self.llm_service.format_diary_entry(
                messages=messages,
                journal_date=today_str,
            )
            if content.strip():
                await self.memory.append_daily(content)

                if trigger == "idle":
                    logger.info(
                        "Diary write [trigger=idle] idle=%.0fs, cursor %d→%d, %d msgs → %d chars",
                        idle_seconds,
                        start_cursor,
                        end_cursor,
                        len(messages),
                        len(content),
                    )
                else:
                    logger.info(
                        "Diary write [trigger=count] cursor %d→%d, %d msgs → %d chars",
                        start_cursor,
                        end_cursor,
                        len(messages),
                        len(content),
                    )
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
        cursor = await asyncio.to_thread(self._read_state_sync)
        self._last_processed_message_id = self._non_negative_int(cursor, 0)

    async def _commit_processed_message_id(self, processed_message_id: int) -> bool:
        normalized_id = self._non_negative_int(processed_message_id, 0)
        try:
            await asyncio.to_thread(self._write_state_sync, normalized_id)
        except Exception as exc:
            logger.error("Failed to persist journal state: %s", exc)
            return False
        self._last_processed_message_id = normalized_id
        return True

    def _read_state_sync(self) -> int:
        path = self._state_path()
        try:
            raw = path.read_text(encoding="utf-8").strip()
            if not raw:
                return 0
            return int(raw)
        except FileNotFoundError:
            return 0
        except (ValueError, OSError) as exc:
            logger.warning("Failed to read journal cursor: %s", exc)
            return 0

    def _write_state_sync(self, cursor: int) -> None:
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(int(cursor)), encoding="utf-8")

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
        return self.memory.root / ".journal_cursor"

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
