"""Memory handler: recent context injection and count-based diary writing."""

from __future__ import annotations

import asyncio
import logging
import os
import re
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
from ..inbox import is_scheduled_work
from ...schemas import Message, MessageType, RoleType

if TYPE_CHECKING:
    from ...components.memory import MarkdownMemory, Note, NoteStore, RelationshipStore
    from ...components.message import MessageStorage
    from ..journal import JournalLLMService

logger = logging.getLogger(__name__)


class MemoryHandler:
    """Manages recent diary context and count-based journal maintenance."""

    RECENT_DAYS = AgentConfig.DIARY_CONTEXT_DAYS
    RECENT_MAX_CHARS = AgentConfig.MEMORY_RECENT_MAX_CHARS
    DEFAULT_JOURNAL_SOURCE_CHARS = 24000  # Soft per-batch source budget; records remain intact.
    EXISTING_TODAY_MAX_CHARS = 4000  # Newest-first trim of today's page for the diary writer.
    DIARY_OMITTED_NOTICE = "earlier diary omitted within recent window"
    _DIARY_HR_RE = re.compile(r"(?m)^---\s*$")
    _DIARY_ENTRY_HEADING_RE = re.compile(r"(?m)^## \d{4}-\d{2}-\d{2} \d{2}:\d{2}\s*$")
    SUBCONSCIOUS_SUMMARY_SCOPES = ("yearly", "monthly", "weekly")
    SUBCONSCIOUS_SUMMARY_CHARS_PER_SCOPE = 2000
    NOTEBOOK_SECTIONS = (
        ("pinned", "[pinned]"),
        ("hubs", "[hubs]"),
        ("relevant", "[relevant to the current message]"),
    )
    NOTEBOOK_OMITTED_NOTICE = "[notes omitted from index due to budget: {count}]"

    def __init__(
        self,
        memory: MarkdownMemory,
        llm_service: JournalLLMService,
        message_storage: MessageStorage,
        *,
        diary_write_batch: int,
        diary_context_days: Optional[int] = None,
        recent_max_chars: Optional[int] = None,
        max_journal_source_chars: Optional[int] = None,
        relationship_store: Optional["RelationshipStore"] = None,
        note_store: Optional["NoteStore"] = None,
        notes_auto_distill: bool = AgentConfig.NOTES_AUTO_DISTILL,
    ) -> None:
        self.memory = memory
        self.llm_service = llm_service
        self.message_storage = message_storage
        self.relationship_store = relationship_store
        self.note_store = note_store
        self.notes_auto_distill = bool(notes_auto_distill)
        self.diary_write_batch = self._positive_int(
            diary_write_batch,
            AgentConfig.DIARY_WRITE_BATCH,
        )
        self.diary_context_days = self._non_negative_int(diary_context_days, self.RECENT_DAYS)
        self.recent_max_chars = self._non_negative_int(recent_max_chars, self.RECENT_MAX_CHARS)
        self.window_overlap = min(
            max(1, int(self.diary_write_batch * AgentConfig.MEMORY_WINDOW_OVERLAP_RATIO)),
            self.diary_write_batch - 1,
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
        days = self.diary_context_days if days is None else self._non_negative_int(days, self.diary_context_days)
        if days <= 0:
            return ""

        entries = await self.memory.read_recent_dailies(days=days)
        if not entries:
            return ""

        sections = [
            (date_str, content.strip())
            for date_str, content in entries
            if content.strip()
        ]
        if not sections:
            return ""
        diary_entries = [
            entry
            for _, content in sections
            for entry in self._split_diary_entries(content)
        ]
        if self.recent_max_chars <= 0:
            return self._join_diary_entries(diary_entries)
        return self._trim_recent_diary_entries(diary_entries, self.recent_max_chars)

    @classmethod
    def _split_diary_entries(cls, content: str) -> list[str]:
        """Split a daily file into whole ``## YYYY-MM-DD HH:MM`` entries.

        Standalone ``---`` rules are ignored; they are leftover separators, not
        part of the diary body.
        """
        text = cls._DIARY_HR_RE.sub("", content or "")
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if not text:
            return []
        matches = list(cls._DIARY_ENTRY_HEADING_RE.finditer(text))
        if not matches:
            return [text]
        entries: list[str] = []
        preamble = text[: matches[0].start()].strip()
        if preamble:
            entries.append(preamble)
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            entry = text[match.start():end].strip()
            if entry:
                entries.append(entry)
        return entries

    @classmethod
    def _join_diary_entries(cls, entries: list[str]) -> str:
        return "\n\n".join(entry.strip() for entry in entries if entry.strip())

    @classmethod
    def _trim_recent_diary_entries(cls, entries: list[str], max_chars: int) -> str:
        """Keep the newest whole diary entries within *max_chars*."""
        if not entries:
            return ""

        max_chars = max(0, int(max_chars))
        if max_chars <= 0:
            return cls._join_diary_entries(entries)

        kept: list[str] = []
        omitted_earlier = False
        for entry in reversed(entries):
            trial = [entry, *kept]
            text = cls._join_diary_entries(trial)
            if not kept or len(text) <= max_chars:
                kept = trial
                if len(text) > max_chars:
                    omitted_earlier = True
                    break
                continue
            omitted_earlier = True
            break

        if len(kept) < len(entries):
            omitted_earlier = True

        parts: list[str] = []
        if omitted_earlier:
            parts.append(cls.DIARY_OMITTED_NOTICE)
        parts.append(cls._join_diary_entries(kept))
        return "\n\n".join(parts)

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
                paths = await self._list_summary_paths(scope)
            except Exception as exc:
                logger.warning("Failed to list %s memory summaries: %s", scope, exc, exc_info=True)
                continue

            for path in reversed(paths):
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

    async def _list_summary_paths(self, scope: str) -> list[Path]:
        """Return real summary files for a scope.

        ``MarkdownMemory.list_files`` returns display labels, not paths, so
        subconscious context has to walk the scope directory itself.
        """
        scope_root = getattr(self.memory, "_scope_root", None)
        if callable(scope_root):
            root = Path(scope_root(scope))
        else:
            root = Path(self.memory.root) / scope
        def _collect() -> list[Path]:
            if not root.exists():
                return []
            return sorted(path for path in root.rglob("*.md") if path.is_file())

        return await asyncio.to_thread(_collect)

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
            if unprocessed_count < self.diary_write_batch - self.window_overlap:
                return False

        # Build a cursor-bounded window that starts window_overlap entries
        # before the last checkpoint (for diary continuity between adjacent
        # batches) and is capped at diary_write_batch (to bound the LLM budget
        # even when many messages accumulate between maintenance cycles).
        # Advancing the checkpoint to the batch end rather than to
        # latest_message_id ensures overflow messages are not dropped.
        start_exclusive = max(0, self._last_processed_message_id - self.window_overlap)
        end_inclusive = min(latest_message_id, start_exclusive + self.diary_write_batch)
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
            jump_to = max(end_inclusive, latest_message_id - self.diary_write_batch)
            await self._commit_processed_message_id(jump_to)
            return False

        checkpoint = self._last_processed_message_id
        new_records = []
        for message in recent_messages:
            if not self._is_memory_worthy_experience(message):
                continue
            record = self._experience_record(message)
            cursor = self._storage_cursor_from_metadata(record.get("metadata"))
            if cursor is not None and cursor <= checkpoint:
                record["already_journaled"] = True
            new_records.append(record)
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
    def _storage_cursor_from_metadata(metadata: object) -> Optional[int]:
        if not isinstance(metadata, dict):
            return None
        raw = metadata.get(AgentConfig.MESSAGE_STORAGE_CURSOR_KEY)
        try:
            cursor = int(raw)
        except (TypeError, ValueError):
            return None
        return cursor if cursor > 0 else None

    @staticmethod
    def _experience_record(message: Message) -> dict:
        metadata = dict(message.metadata or {})
        return {
            "role": message.role.value,
            "type": message.type.value,
            "sender_id": message.sender_id,
            "content": message.content,
            "timestamp": message.timestamp,
            "channel": message.channel,
            "room_name": message.room_name,
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
            existing_by_key = {}
            for participant in participants:
                card = await store.read_card(participant["key"])
                if card is None:
                    continue
                existing_by_key[participant["key"]] = card
                if not card.is_empty:
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

            from ...components.memory import RelationshipCard, human_display_name

            today_str = date.today().isoformat()
            participant_by_key = {p["key"]: p for p in participants}
            for key, body in new_cards.items():
                participant = participant_by_key.get(key, {})
                user_id = str(participant.get("user_id") or "")
                display_name = human_display_name(
                    participant.get("display_name"),
                    user_id=user_id,
                    key=key,
                )
                existing = existing_by_key.get(key)
                if not display_name and existing is not None:
                    display_name = human_display_name(
                        existing.display_name,
                        user_id=existing.user_id,
                        key=existing.key,
                    )
                await store.write_card(
                    RelationshipCard(
                        key=key,
                        body=body,
                        display_name=display_name,
                        channel=str(participant.get("channel") or ""),
                        user_id=user_id,
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
        from ...components.memory import RelationshipStore, human_display_name

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
            if not channel:
                # Incomplete identity must not mint unknown:* parallel keys.
                continue
            if is_scheduled_work(message.metadata):
                # Synthetic work-order prompts are not human speech.
                continue
            metadata = message.metadata or {}
            key = RelationshipStore.make_key(channel, user_id)
            display_name = human_display_name(
                metadata.get("sender_name"),
                user_id=user_id,
                key=key,
            )
            existing = participants.get(key)
            if existing is not None:
                if display_name and not existing["display_name"]:
                    existing["display_name"] = display_name
                continue
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

        from ...components.memory import anonymous_contact_label, human_display_name

        blocks: list[str] = []
        for card in cards:
            name = human_display_name(
                card.display_name,
                user_id=card.user_id,
                key=card.key,
            ) or anonymous_contact_label(card.channel)
            body = card.body.strip()
            if include_routing_id and card.user_id:
                header = f"## {name} [user_id: {card.user_id}]"
            else:
                header = f"## {name}"
            blocks.append(f"{header}\n{body}")
        return "\n\n".join(blocks)

    # ------------------------------------------------------------------
    # Notebook (topic-addressed projection over the diary)
    # ------------------------------------------------------------------

    async def _distill_notes_from_weekly(
        self,
        diary_source: str,
        week_start: date,
        week_end: date,
        week_arc: str = "",
        period_label: str = "",
    ) -> None:
        """Distil reusable conclusions after a weekly summary is written.

        The weekly file is only the processing-session latch. Feedstock is the
        week's diary range (already fetched for the summary); the summary body
        is optional orientation, not the sole upstream. Best-effort: a failure
        here must never roll back the weekly summary.
        """
        if self.note_store is None or not self.notes_auto_distill:
            return
        if not str(diary_source or "").strip():
            return
        try:
            from ...components.memory import Note, SENSITIVITY_SHAREABLE

            existing = await self.note_store.list_notes()
            snippet_cap = AgentConfig.NOTEBOOK_SNIPPET_MAX_CHARS
            candidates = await self.llm_service.distill_notes(
                diary_source=diary_source,
                week_arc=week_arc,
                period_label=period_label,
                existing_notes=[
                    {
                        "id": note.id,
                        "title": note.title,
                        "tags": list(note.tags),
                        "keys": list(note.keys),
                        "snippet": (note.snippet or "")[:snippet_cap],
                    }
                    for note in existing[: AgentConfig.NOTES_DISTILL_CONTEXT_NOTES]
                ],
                max_notes=AgentConfig.NOTES_DISTILL_MAX_PER_WEEK,
                max_diary_chars=self.max_journal_source_chars,
            )
            if not candidates:
                return

            known_ids = {note.id for note in existing}
            today_str = date.today().isoformat()
            diary_range = [week_start.isoformat(), week_end.isoformat()]

            written: list[str] = []
            for candidate in candidates[: AgentConfig.NOTES_DISTILL_MAX_PER_WEEK]:
                title = str(candidate.get("title") or "").strip()
                body = str(candidate.get("body") or "").strip()
                if not title or not body:
                    continue
                keys = candidate.get("keys")
                tags = candidate.get("tags")
                duplicate = await self._existing_note_covering(
                    title=title,
                    keys=keys,
                    tags=tags,
                )
                if duplicate is not None:
                    logger.debug(
                        "Skipped distilled note %r; note %s already covers it",
                        title,
                        duplicate.id,
                    )
                    continue

                model_links = [
                    link_id
                    for link_id in (candidate.get("links") or [])
                    if str(link_id).strip() in known_ids
                ]
                neighbour_links = await self._mechanical_neighbour_links(
                    title=title,
                    keys=keys,
                    tags=tags,
                )
                links = self._merge_link_ids(model_links, neighbour_links)

                note = await self.note_store.create(
                    Note(
                        id="",
                        title=title,
                        body=body,
                        tags=tuple(tags or ()),
                        keys=tuple(keys or ()),
                        links=tuple(links),
                        sensitivity=SENSITIVITY_SHAREABLE,
                        source={"diary": list(diary_range)},
                        created=today_str,
                        updated=today_str,
                    )
                )
                known_ids.add(note.id)
                written.append(f"{note.id} ({len(note.body)} chars)")
            if written:
                logger.info(
                    "Distilled %d note(s) from weekly %s: %s",
                    len(written),
                    period_label or f"{week_start}..{week_end}",
                    ", ".join(written),
                )
        except Exception as exc:
            logger.warning("Note distillation failed: %s", exc, exc_info=True)

    async def _mechanical_neighbour_links(
        self,
        title: str,
        keys=None,
        tags=None,
    ) -> List[str]:
        """Return related note ids below the duplicate threshold for write-time linking."""
        if self.note_store is None:
            return []
        from ...components.memory import NoteStore

        similar = await self.note_store.find_similar(
            title=title,
            keys=keys,
            tags=tags,
            limit=AgentConfig.NOTES_MECHANICAL_LINK_MAX + 2,
        )
        linked: List[str] = []
        for candidate in similar:
            score = NoteStore.identity_score(candidate, title, keys, tags)
            if score >= AgentConfig.NOTES_DUPLICATE_SCORE_THRESHOLD:
                continue
            if score < AgentConfig.NOTES_LINK_SCORE_THRESHOLD:
                continue
            linked.append(candidate.id)
            if len(linked) >= AgentConfig.NOTES_MECHANICAL_LINK_MAX:
                break
        return linked

    @staticmethod
    def _merge_link_ids(*groups) -> List[str]:
        merged: List[str] = []
        for group in groups:
            for value in group or ():
                link_id = str(value or "").strip()
                if link_id and link_id not in merged:
                    merged.append(link_id)
        return merged

    async def _garden_notes_after_monthly(self, period_label: str = "") -> None:
        """Mechanical gardening after a monthly summary is written.

        Best-effort: never rolls back the monthly summary. Controlled by the
        same ``notes_auto_distill`` switch as weekly distillation.
        """
        if self.note_store is None or not self.notes_auto_distill:
            return
        try:
            linked = await self._link_orphan_notes()
            hubs = await self._ensure_tag_hubs()
            if linked or hubs:
                logger.info(
                    "Notebook gardening after monthly %s: linked %d orphan(s), "
                    "created/updated %d hub(s)",
                    period_label or "period",
                    linked,
                    hubs,
                )
        except Exception as exc:
            logger.warning("Notebook gardening failed: %s", exc, exc_info=True)

    async def _link_orphan_notes(self) -> int:
        """Attach 1–2 neighbour links to active notes with no links and no backlinks."""
        if self.note_store is None:
            return 0
        from ...components.memory import NoteStore
        from dataclasses import replace

        notes = await self.note_store.list_notes()
        if len(notes) < 2:
            return 0

        linked_count = 0
        today_str = date.today().isoformat()
        for note in notes:
            if note.links:
                continue
            if await self.note_store.backlinks(note.id):
                continue
            similar = await self.note_store.find_similar(
                title=note.title,
                keys=note.keys,
                tags=note.tags,
                limit=AgentConfig.NOTES_MECHANICAL_LINK_MAX + 1,
            )
            neighbour_ids: List[str] = []
            for candidate in similar:
                if candidate.id == note.id:
                    continue
                score = NoteStore.identity_score(
                    candidate, note.title, note.keys, note.tags
                )
                if score < AgentConfig.NOTES_LINK_SCORE_THRESHOLD:
                    continue
                neighbour_ids.append(candidate.id)
                if len(neighbour_ids) >= AgentConfig.NOTES_MECHANICAL_LINK_MAX:
                    break
            if not neighbour_ids:
                continue
            updated = self.note_store.normalize(
                replace(note, links=tuple(neighbour_ids), updated=today_str)
            )
            await self.note_store.write(updated)
            linked_count += 1
        return linked_count

    async def _ensure_tag_hubs(self) -> int:
        """Create or refresh hub notes for tags that reach the cluster threshold."""
        if self.note_store is None:
            return 0
        from ...components.memory import KIND_HUB, Note
        from dataclasses import replace

        notes = await self.note_store.list_notes()
        by_tag: dict[str, list] = {}
        hubs = [note for note in notes if note.kind == KIND_HUB]
        for note in notes:
            if note.kind == KIND_HUB:
                continue
            for tag in note.tags:
                folded = tag.casefold()
                by_tag.setdefault(folded, []).append(note)

        today_str = date.today().isoformat()
        touched = 0
        for folded, members in by_tag.items():
            if len(members) < AgentConfig.NOTES_HUB_MIN_CLUSTER:
                continue
            # Prefer the first member's original casing for the hub title.
            display_tag = next(
                (tag for note in members for tag in note.tags if tag.casefold() == folded),
                folded,
            )
            member_ids = [note.id for note in members]
            body_lines = [
                f"- [[{note.id}]] {note.title}" for note in members
            ]
            body = "\n".join(body_lines)

            existing_hub = None
            for hub in hubs:
                hub_labels = {label.casefold() for label in (*hub.tags, *hub.keys, hub.title)}
                if folded in hub_labels:
                    existing_hub = hub
                    break

            if existing_hub is not None:
                updated = self.note_store.normalize(
                    replace(
                        existing_hub,
                        body=body,
                        links=tuple(member_ids),
                        tags=tuple(
                            dict.fromkeys([*(existing_hub.tags), display_tag])
                        ),
                        keys=tuple(
                            dict.fromkeys([*(existing_hub.keys), display_tag])
                        ),
                        updated=today_str,
                    )
                )
                await self.note_store.write(updated)
                touched += 1
                continue

            hub = await self.note_store.create(
                Note(
                    id="",
                    title=display_tag,
                    body=body,
                    kind=KIND_HUB,
                    tags=(display_tag,),
                    keys=(display_tag,),
                    links=tuple(member_ids),
                    source={"diary": [today_str]},
                    created=today_str,
                    updated=today_str,
                )
            )
            hubs.append(hub)
            touched += 1
        return touched

    async def _existing_note_covering(
        self,
        title: str,
        keys=None,
        tags=None,
    ) -> Optional["Note"]:
        """Return an existing note that already covers this idea, if any.

        Uses the same threshold the write tool applies, so a draft the agent
        would have been told to fold into an existing note is not quietly
        written by the background path instead.
        """
        if self.note_store is None:
            return None
        from ...components.memory import NoteStore

        candidates = await self.note_store.find_similar(
            title=title,
            keys=keys,
            tags=tags,
            limit=3,
        )
        for candidate in candidates:
            score = NoteStore.identity_score(candidate, title, keys, tags)
            if score >= AgentConfig.NOTES_DUPLICATE_SCORE_THRESHOLD:
                return candidate
        return None

    async def get_notebook_context(self, current_text: str = "") -> str:
        """Render the notebook index for prompt injection.

        Deliberately an index, not the notebook: pinned notes carry their body
        because pinning means "keep this in mind", while hubs and recalled notes
        carry a title and one snippet line so the model can decide whether to
        open them with ``read_note``.
        """
        if self.note_store is None:
            return ""

        try:
            pinned = await self.note_store.pinned(limit=AgentConfig.NOTEBOOK_PINNED_MAX)
            hubs = await self.note_store.hubs(limit=AgentConfig.NOTEBOOK_HUB_MAX)
            recalled = (
                await self.note_store.recall(
                    current_text,
                    limit=AgentConfig.NOTEBOOK_RELEVANT_MAX,
                )
                if str(current_text or "").strip()
                else []
            )
        except Exception as exc:
            logger.warning("Failed to read notebook: %s", exc, exc_info=True)
            return ""

        return self._render_notebook_sections(pinned=pinned, hubs=hubs, recalled=recalled)

    @classmethod
    def _render_notebook_sections(
        cls,
        pinned: List["Note"],
        hubs: List["Note"],
        recalled: List["Note"],
    ) -> str:
        """Assemble notebook index rows within the prompt budget.

        Rows are selected in priority order (pinned, then recalled, then hubs)
        and rendered in reading order, so a tight budget drops navigation rows
        before it drops a note the current message actually matched.
        """
        shown: set[str] = set()
        candidates: list[tuple[str, "Note", int]] = []
        for note in pinned:
            if note.id in shown:
                continue
            shown.add(note.id)
            candidates.append(("pinned", note, AgentConfig.NOTEBOOK_PINNED_BODY_MAX_CHARS))
        for note in recalled:
            if note.id in shown:
                continue
            shown.add(note.id)
            candidates.append(("relevant", note, AgentConfig.NOTEBOOK_SNIPPET_MAX_CHARS))
        for note in hubs:
            if note.id in shown:
                continue
            shown.add(note.id)
            candidates.append(("hubs", note, 0))

        if not candidates:
            return ""

        # Reserve the framing (section headings plus a possible omission
        # notice) before budgeting rows, so the returned block really does fit.
        reserve = sum(len(heading) + 1 for _section, heading in cls.NOTEBOOK_SECTIONS)
        reserve += len(cls.NOTEBOOK_OMITTED_NOTICE.format(count=len(candidates))) + 1
        budget = max(1, int(AgentConfig.NOTEBOOK_CONTEXT_MAX_CHARS) - reserve)

        rows: dict[str, list[str]] = {section: [] for section, _heading in cls.NOTEBOOK_SECTIONS}
        used = 0
        omitted = 0
        for section, note, body_limit in candidates:
            row = cls._format_notebook_row(note, body_limit)
            if used + len(row) + 1 > budget:
                omitted += 1
                continue
            rows[section].append(row)
            used += len(row) + 1

        blocks: list[str] = []
        for section, heading in cls.NOTEBOOK_SECTIONS:
            if rows[section]:
                blocks.append("\n".join([heading, *rows[section]]))
        if not blocks:
            return ""
        if omitted:
            blocks.append(cls.NOTEBOOK_OMITTED_NOTICE.format(count=omitted))
        return "\n".join(blocks)

    @staticmethod
    def _format_notebook_row(note: "Note", body_limit: int) -> str:
        from ...components.memory import KIND_HUB, SENSITIVITY_SHAREABLE

        header = f"- ({note.id}) {note.title}"
        if note.sensitivity != SENSITIVITY_SHAREABLE:
            header += f" [{note.sensitivity}]"
        if note.kind == KIND_HUB and note.links:
            header += f" [{len(note.links)} linked]"
        if body_limit <= 0:
            return header
        text = " ".join((note.body or "").split())
        if not text:
            return header
        if len(text) > body_limit:
            text = text[:body_limit].rstrip() + "..."
        return f"{header}\n  {text}"

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
        """LLM-format messages and append today's next diary slice.

        Empty model output means the slice is already covered: do not append,
        but still succeed so the checkpoint can advance. LLM errors return
        False so the checkpoint stays put.
        """
        today = date.today()
        today_str = today.isoformat()
        existing_today = await self._read_existing_today(today)

        try:
            content = await self.llm_service.format_diary_entry(
                messages=messages,
                journal_date=today_str,
                existing_today=existing_today,
            )
            body = content.strip()
            if body:
                await self.memory.append_daily(body)

            if trigger == "idle":
                logger.info(
                    "Diary write [trigger=idle] idle=%.0fs, cursor %d→%d, %d msgs → %d chars%s",
                    idle_seconds,
                    start_cursor,
                    end_cursor,
                    len(messages),
                    len(body),
                    "" if body else " (empty slice)",
                )
            else:
                logger.info(
                    "Diary write [trigger=count] cursor %d→%d, %d msgs → %d chars%s",
                    start_cursor,
                    end_cursor,
                    len(messages),
                    len(body),
                    "" if body else " (empty slice)",
                )
            return True
        except Exception as exc:
            logger.error("Background diary write failed: %s", exc)
        return False

    async def _read_existing_today(self, target_date: date) -> str:
        """Return today's daily prose, newest-first trimmed for the diary writer."""
        path = self.memory.daily_path(target_date)
        text = await self.memory.read_file(path)
        if not text.strip():
            return ""
        entries = self._split_diary_entries(text)
        if not entries:
            return ""
        trimmed = self._trim_recent_diary_entries(entries, self.EXISTING_TODAY_MAX_CHARS)
        # Drop the prompt-budget notice; the writer should see diary prose only.
        if trimmed.startswith(self.DIARY_OMITTED_NOTICE):
            trimmed = trimmed[len(self.DIARY_OMITTED_NOTICE):].lstrip("\n")
        return trimmed.strip()

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
        await self._generate_previous_monthly_summary_if_missing_locked(today=today)
        await self._generate_previous_yearly_summary_if_missing_locked(today=today)

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

    async def generate_previous_monthly_summary_if_missing(
        self,
        today: Optional[date] = None,
    ) -> bool:
        """Generate the previous completed month's summary if it is missing."""
        async with self._maintenance_guard():
            return await self._generate_previous_monthly_summary_if_missing_locked(today=today)

    async def _generate_previous_monthly_summary_if_missing_locked(
        self,
        today: Optional[date] = None,
    ) -> bool:
        last_year, last_month = self._previous_month(today or date.today())
        monthly_path = self.memory.monthly_path(last_year, last_month)
        if monthly_path.exists():
            return False
        return await self._generate_monthly(last_year, last_month)

    async def generate_previous_yearly_summary_if_missing(
        self,
        today: Optional[date] = None,
    ) -> bool:
        """Generate the previous completed year's summary if it is missing."""
        async with self._maintenance_guard():
            return await self._generate_previous_yearly_summary_if_missing_locked(today=today)

    async def _generate_previous_yearly_summary_if_missing_locked(
        self,
        today: Optional[date] = None,
    ) -> bool:
        target_year = (today or date.today()).year - 1
        yearly_path = self.memory.yearly_path(target_year)
        if yearly_path.exists():
            return False
        return await self._generate_yearly(target_year)

    @staticmethod
    def _previous_month(current_day: date) -> tuple[int, int]:
        if current_day.month == 1:
            return current_day.year - 1, 12
        return current_day.year, current_day.month - 1

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
            await self._distill_notes_from_weekly(
                diary_source=source,
                week_arc=summary,
                week_start=week_start,
                week_end=week_end,
                period_label=label,
            )
            return True
        return False

    async def _generate_monthly(self, year: int, month: int) -> bool:
        import calendar

        first = date(year, month, 1)
        last = date(year, month, calendar.monthrange(year, month)[1])
        source = await self.memory.search_date_range(
            start=first.isoformat(),
            end=last.isoformat(),
        )
        if not source.strip():
            return False
        label = f"{year}-{month:02d}"
        summary = await self.llm_service.generate_summary(source, "monthly", label)
        if summary:
            await self.memory.write_summary(self.memory.monthly_path(year, month), summary)
            logger.info("Generated monthly summary: %s", label)
            await self._garden_notes_after_monthly(period_label=label)
            return True
        return False

    async def _generate_yearly(self, year: int) -> bool:
        parts: list[str] = []
        for month in range(1, 13):
            text = await self.memory.read_file(self.memory.monthly_path(year, month))
            if text.strip():
                parts.append(f"# {year}-{month:02d}\n\n{text}")
        source = "\n\n".join(parts)
        if not source.strip():
            return False
        label = str(year)
        summary = await self.llm_service.generate_summary(source, "yearly", label)
        if summary:
            await self.memory.write_summary(self.memory.yearly_path(year), summary)
            logger.info("Generated yearly summary: %s", label)
            return True
        return False

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
        tmp_path = path.with_name(f".{path.name}.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(str(int(cursor)))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)

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
