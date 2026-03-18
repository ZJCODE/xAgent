import asyncio
import logging
import re
import sqlite3
import string
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ...schemas import Message, MessageType
from .base_memory import MemoryStorageBase
from .config.memory_config import (
    EXPLICIT_MEMORY_PATTERNS,
    MEMORY_EXTRACTION_INTERVAL_SECONDS,
    MEMORY_FORCE_EXTRACTION_MULTIPLIER,
    MEMORY_MAX_BATCH_MESSAGES,
)
from .helper.llm_service import JournalLLMService


@dataclass
class StreamMemoryState:
    """Persistent extraction cursor and schedule state for one memory stream."""

    last_processed_message_id: int = 0
    last_written_at: float = 0.0


class MemoryStorageBasic(MemoryStorageBase):
    """SQLite-backed daily journal memory built on the agent message database."""

    DEFAULT_PATH = "~/.xagent/messages.sqlite3"
    CONNECT_TIMEOUT = 5.0
    MESSAGES_TABLE = "messages"
    JOURNALS_TABLE = "journals"
    JOURNAL_STATE_TABLE = "journal_state"
    JOURNAL_FTS_TABLE = "journal_fts"

    def __init__(
        self,
        path: Optional[str] = None,
        memory_threshold: int = 10,
        message_storage=None,
        memory_interval_seconds: int = MEMORY_EXTRACTION_INTERVAL_SECONDS,
        max_batch_messages: int = MEMORY_MAX_BATCH_MESSAGES,
    ):
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        self.llm_service = JournalLLMService()
        self.message_storage = None
        self.path: Optional[Path] = None
        self.memory_threshold = max(1, memory_threshold)
        self.memory_interval_seconds = max(0, memory_interval_seconds)
        self.max_batch_messages = max(1, max_batch_messages)
        self.force_extraction_threshold = max(
            self.memory_threshold + 1,
            self.memory_threshold * MEMORY_FORCE_EXTRACTION_MULTIPLIER,
        )
        self._stream_locks: Dict[str, asyncio.Lock] = {}
        self._speaker_aliases: Dict[tuple[str, str], Dict[str, str]] = {}
        self._explicit_memory_patterns = [
            re.compile(pattern, re.IGNORECASE) for pattern in EXPLICIT_MEMORY_PATTERNS
        ]
        self.logger.debug(
            "Initializing journal memory: path=%s threshold=%d interval_seconds=%d max_batch_messages=%d force_threshold=%d",
            path,
            self.memory_threshold,
            self.memory_interval_seconds,
            self.max_batch_messages,
            self.force_extraction_threshold,
        )

        explicit_path = Path(path).expanduser().resolve() if path else None
        if explicit_path is not None:
            self.path = explicit_path
            self._initialize_database()

        if message_storage is not None:
            self.bind_message_storage(message_storage)
        elif self.path is None:
            self.path = Path(self.DEFAULT_PATH).expanduser().resolve()
            self._initialize_database()

    def bind_message_storage(self, message_storage) -> None:
        """Bind a message storage instance so journal memory can share the same SQLite file."""
        self.message_storage = message_storage
        candidate_path = self._extract_storage_path(message_storage)
        self.logger.debug(
            "Binding message storage to journal memory: storage_type=%s storage_path=%s current_path=%s",
            type(message_storage).__name__,
            candidate_path,
            self.path,
        )
        if candidate_path is None:
            if self.path is None:
                raise ValueError(
                    "SQLite journal memory requires a message storage with a local SQLite path "
                    "or an explicit MemoryStorageLocal(path=...)."
                )
            self.logger.debug(
                "Message storage has no local path; continuing with explicit journal db path=%s",
                self.path,
            )
            return

        if self.path is None:
            self.path = candidate_path
            self._initialize_database()
            self.logger.info("Journal memory bound to shared sqlite path: %s", self.path)
            return

        if candidate_path != self.path:
            raise ValueError(
                f"Memory storage path {self.path} does not match message storage path {candidate_path}."
            )
        self.logger.debug("Journal memory confirmed shared sqlite path: %s", self.path)

    async def add(
        self,
        memory_key: str,
        messages: List[Dict[str, Any]],
    ) -> None:
        """Rewrite per-day journals from unread transcript batches."""
        if not messages:
            self.logger.debug("Skipping journal add for %s: empty message batch", memory_key)
            return

        self._ensure_ready()
        explicit_trigger = self._contains_explicit_memory_intent(self._extract_user_messages(messages))
        self.logger.debug(
            "Journal add requested: memory_key=%s incoming_messages=%d explicit_trigger=%s",
            memory_key,
            len(messages),
            explicit_trigger,
        )

        async with self._stream_lock(memory_key):
            state = await asyncio.to_thread(self._get_state_sync, memory_key)
            unread_count = await asyncio.to_thread(
                self._count_unprocessed_messages_sync,
                state.last_processed_message_id,
            )
            self.logger.debug(
                "Journal state loaded: memory_key=%s last_processed_message_id=%d last_written_at=%.3f unread_count=%d",
                memory_key,
                state.last_processed_message_id,
                state.last_written_at,
                unread_count,
            )
            if unread_count <= 0:
                self.logger.debug("Skipping journal add for %s: no unread messages", memory_key)
                return

            if not explicit_trigger and not self._should_extract(state, unread_count):
                self.logger.debug(
                    "Skipping journal add for %s: thresholds not met (threshold=%d force_threshold=%d interval_seconds=%d)",
                    memory_key,
                    self.memory_threshold,
                    self.force_extraction_threshold,
                    self.memory_interval_seconds,
                )
                return

            batch_rows = await asyncio.to_thread(
                self._fetch_unprocessed_message_rows_sync,
                state.last_processed_message_id,
                self.max_batch_messages,
            )
            if not batch_rows:
                self.logger.debug(
                    "Skipping journal add for %s: unread count existed but fetched batch was empty",
                    memory_key,
                )
                return
            self.logger.debug(
                "Fetched unread journal batch: memory_key=%s rows=%d first_id=%d last_id=%d",
                memory_key,
                len(batch_rows),
                batch_rows[0]["id"],
                batch_rows[-1]["id"],
            )

            wrote_any = False
            grouped = self._group_messages_by_date(batch_rows)
            self.logger.debug(
                "Grouped journal batch by date for %s: %s",
                memory_key,
                {journal_date: len(grouped_rows) for journal_date, grouped_rows in grouped.items()},
            )
            for journal_date, grouped_rows in grouped.items():
                transcript = self._format_messages_for_storage(
                    memory_key=memory_key,
                    journal_date=journal_date,
                    messages=grouped_rows,
                )
                if not transcript:
                    self.logger.debug(
                        "Skipping journal rewrite for %s on %s: no journal-eligible transcript after filtering",
                        memory_key,
                        journal_date,
                    )
                    continue

                existing_journal = await asyncio.to_thread(
                    self._get_journal_content_sync,
                    memory_key,
                    journal_date,
                )
                self.logger.debug(
                    "Rewriting journal: memory_key=%s journal_date=%s existing_len=%d transcript_len=%d grouped_rows=%d",
                    memory_key,
                    journal_date,
                    len(existing_journal),
                    len(transcript),
                    len(grouped_rows),
                )
                rewritten = await self.llm_service.rewrite_daily_journal(
                    existing_journal=existing_journal,
                    new_transcript=transcript,
                    journal_date=journal_date,
                )
                normalized = self._normalise_text(rewritten)
                if not normalized:
                    self.logger.warning(
                        "Journal rewrite returned empty content: memory_key=%s journal_date=%s",
                        memory_key,
                        journal_date,
                    )
                    continue

                journal_id = await asyncio.to_thread(
                    self._upsert_journal_sync,
                    memory_key,
                    journal_date,
                    normalized,
                )
                wrote_any = True
                self.logger.info(
                    "Journal updated: memory_key=%s journal_date=%s journal_id=%s content_len=%d",
                    memory_key,
                    journal_date,
                    journal_id,
                    len(normalized),
                )

            await asyncio.to_thread(
                self._upsert_state_sync,
                memory_key,
                batch_rows[-1]["id"],
                time.time() if wrote_any or batch_rows else state.last_written_at,
            )
            self.logger.debug(
                "Journal state advanced: memory_key=%s last_processed_message_id=%d wrote_any=%s",
                memory_key,
                batch_rows[-1]["id"],
                wrote_any,
            )

    async def store(
        self,
        memory_key: str,
        content: str,
    ) -> str:
        """Directly upsert the current day's journal content."""
        self._ensure_ready()
        normalized = self._normalise_text(content)
        if not normalized:
            self.logger.debug("Skipping direct journal store for %s: empty content", memory_key)
            return ""

        journal_date = self._current_local_date()
        journal_id = await asyncio.to_thread(
            self._upsert_journal_sync,
            memory_key,
            journal_date,
            normalized,
        )
        self.logger.info(
            "Direct journal store: memory_key=%s journal_date=%s journal_id=%s content_len=%d",
            memory_key,
            journal_date,
            journal_id,
            len(normalized),
        )
        return str(journal_id)

    async def retrieve(
        self,
        memory_key: str,
        query: str = "",
        limit: int = 5,
        journal_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve journal entries by date, keywords, or both."""
        self._ensure_ready()
        if limit <= 0:
            raise ValueError("limit must be a positive integer")

        query = str(query or "").strip()
        normalized_date = self._normalize_journal_date(journal_date)
        self.logger.debug(
            "Journal retrieve requested: memory_key=%s query=%r limit=%d journal_date=%s",
            memory_key,
            query,
            limit,
            normalized_date,
        )

        if normalized_date and not query:
            entry = await asyncio.to_thread(
                self._get_journal_entry_by_date_sync,
                memory_key,
                normalized_date,
            )
            self.logger.info(
                "Journal retrieve by date: memory_key=%s journal_date=%s found=%s",
                memory_key,
                normalized_date,
                bool(entry),
            )
            return [entry] if entry else []

        if not query:
            entries = await asyncio.to_thread(
                self._get_latest_journal_entries_sync,
                memory_key,
                limit,
            )
            self.logger.info(
                "Journal retrieve latest: memory_key=%s limit=%d results=%d",
                memory_key,
                limit,
                len(entries),
            )
            return entries

        keywords = await self.llm_service.extract_query_keywords(query=query)
        if not keywords:
            keywords = [query]
            self.logger.debug(
                "Keyword extraction returned empty set; falling back to raw query for %s",
                memory_key,
            )
        self.logger.debug(
            "Journal retrieve keywords: memory_key=%s keywords=%s",
            memory_key,
            keywords,
        )

        results = await asyncio.to_thread(
            self._search_journals_sync,
            memory_key,
            keywords,
            limit,
            normalized_date,
        )
        self.logger.info(
            "Journal retrieve search: memory_key=%s query=%r journal_date=%s keywords=%s results=%d payload=%s",
            memory_key,
            query,
            normalized_date,
            keywords,
            len(results),
            self._summarize_results_for_log(results),
        )
        return results

    async def clear(self, memory_key: str) -> None:
        self._ensure_ready()
        await asyncio.to_thread(self._clear_sync, memory_key)
        self.logger.info("Cleared journal memory for %s", memory_key)

    async def delete(self, memory_ids: List[str]) -> None:
        self._ensure_ready()
        if not memory_ids:
            self.logger.debug("Skipping journal delete: empty id list")
            return
        journal_ids = [int(memory_id) for memory_id in memory_ids]
        await asyncio.to_thread(self._delete_sync, journal_ids)
        self.logger.info("Deleted journal rows by id: %s", journal_ids)

    def _ensure_ready(self) -> None:
        if self.path is None:
            raise ValueError(
                "SQLite journal memory is not configured with a database path."
            )

    def _connect(self) -> sqlite3.Connection:
        self._ensure_ready()
        conn = sqlite3.connect(str(self.path), timeout=self.CONNECT_TIMEOUT)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize_database(self) -> None:
        self._ensure_ready()
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.logger.debug("Initializing journal database schema at %s", self.path)

        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(f"DROP TRIGGER IF EXISTS trg_{self.JOURNALS_TABLE}_ai")
            conn.execute(f"DROP TRIGGER IF EXISTS trg_{self.JOURNALS_TABLE}_ad")
            conn.execute(f"DROP TRIGGER IF EXISTS trg_{self.JOURNALS_TABLE}_au")
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.JOURNALS_TABLE} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_key TEXT NOT NULL,
                    journal_date TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(memory_key, journal_date)
                )
                """
            )
            conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_{self.JOURNALS_TABLE}_memory_key_date
                ON {self.JOURNALS_TABLE} (memory_key, journal_date)
                """
            )
            conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_{self.JOURNALS_TABLE}_updated_at
                ON {self.JOURNALS_TABLE} (updated_at DESC)
                """
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.JOURNAL_STATE_TABLE} (
                    memory_key TEXT PRIMARY KEY,
                    last_processed_message_id INTEGER NOT NULL DEFAULT 0,
                    last_written_at REAL NOT NULL DEFAULT 0.0
                )
                """
            )
            conn.execute(
                f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS {self.JOURNAL_FTS_TABLE}
                USING fts5(
                    journal_id UNINDEXED,
                    memory_key UNINDEXED,
                    journal_date UNINDEXED,
                    content,
                    tokenize='trigram'
                )
                """
            )
            conn.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS trg_{self.JOURNALS_TABLE}_ai
                AFTER INSERT ON {self.JOURNALS_TABLE}
                BEGIN
                    INSERT INTO {self.JOURNAL_FTS_TABLE}(
                        rowid,
                        journal_id,
                        memory_key,
                        journal_date,
                        content
                    )
                    VALUES (
                        NEW.id,
                        NEW.id,
                        NEW.memory_key,
                        NEW.journal_date,
                        NEW.content
                    );
                END
                """
            )
            conn.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS trg_{self.JOURNALS_TABLE}_ad
                AFTER DELETE ON {self.JOURNALS_TABLE}
                BEGIN
                    DELETE FROM {self.JOURNAL_FTS_TABLE} WHERE rowid = OLD.id;
                END
                """
            )
            conn.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS trg_{self.JOURNALS_TABLE}_au
                AFTER UPDATE ON {self.JOURNALS_TABLE}
                BEGIN
                    DELETE FROM {self.JOURNAL_FTS_TABLE} WHERE rowid = OLD.id;
                    INSERT INTO {self.JOURNAL_FTS_TABLE}(
                        rowid,
                        journal_id,
                        memory_key,
                        journal_date,
                        content
                    )
                    VALUES (
                        NEW.id,
                        NEW.id,
                        NEW.memory_key,
                        NEW.journal_date,
                        NEW.content
                    );
                END
                """
            )
            conn.execute(f"DELETE FROM {self.JOURNAL_FTS_TABLE}")
            conn.execute(
                f"""
                INSERT INTO {self.JOURNAL_FTS_TABLE}(rowid, journal_id, memory_key, journal_date, content)
                SELECT id, id, memory_key, journal_date, content
                FROM {self.JOURNALS_TABLE}
                """
            )
            conn.commit()
        self.logger.info("Journal database ready at %s", self.path)

    def _get_state_sync(self, memory_key: str) -> StreamMemoryState:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT last_processed_message_id, last_written_at
                FROM {self.JOURNAL_STATE_TABLE}
                WHERE memory_key = ?
                """,
                (memory_key,),
            ).fetchone()

        if row is None:
            self.logger.debug("No persisted journal state for %s; using defaults", memory_key)
            return StreamMemoryState()

        return StreamMemoryState(
            last_processed_message_id=int(row["last_processed_message_id"] or 0),
            last_written_at=float(row["last_written_at"] or 0.0),
        )

    def _upsert_state_sync(
        self,
        memory_key: str,
        last_processed_message_id: int,
        last_written_at: float,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO {self.JOURNAL_STATE_TABLE} (
                    memory_key,
                    last_processed_message_id,
                    last_written_at
                )
                VALUES (?, ?, ?)
                ON CONFLICT(memory_key) DO UPDATE SET
                    last_processed_message_id = excluded.last_processed_message_id,
                    last_written_at = excluded.last_written_at
                """,
                (memory_key, int(last_processed_message_id), float(last_written_at)),
            )
            conn.commit()

    def _count_unprocessed_messages_sync(self, last_processed_message_id: int) -> int:
        if not self._messages_table_exists_sync():
            self.logger.debug(
                "Messages table missing at %s; unread journal count defaults to 0",
                self.path,
            )
            return 0

        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS unread_count
                FROM {self.MESSAGES_TABLE}
                WHERE id > ?
                """,
                (int(last_processed_message_id),),
            ).fetchone()
        return int(row["unread_count"]) if row is not None else 0

    def _fetch_unprocessed_message_rows_sync(
        self,
        last_processed_message_id: int,
        limit: int,
    ) -> List[Dict[str, Any]]:
        if not self._messages_table_exists_sync():
            self.logger.debug(
                "Messages table missing at %s; no unread journal rows available",
                self.path,
            )
            return []

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, message_json
                FROM {self.MESSAGES_TABLE}
                WHERE id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (int(last_processed_message_id), int(limit)),
            ).fetchall()

        serialised: List[Dict[str, Any]] = []
        for row in rows:
            item: Dict[str, Any] = {"id": int(row["id"])}
            try:
                message = Message.model_validate_json(row["message_json"])
                item.update(
                    {
                        "role": getattr(message.role, "value", message.role),
                        "type": getattr(message.type, "value", message.type),
                        "content": message.content,
                        "timestamp": message.timestamp,
                        "sender_id": message.sender_id,
                    }
                )
            except Exception as exc:
                self.logger.warning("Skipping invalid journal-source message %s: %s", row["id"], exc)
            serialised.append(item)
        self.logger.debug(
            "Decoded unread journal rows from sqlite: rows=%d decoded=%d",
            len(rows),
            len(serialised),
        )
        return serialised

    def _group_messages_by_date(
        self,
        messages: Sequence[Dict[str, Any]],
    ) -> Dict[str, List[Dict[str, Any]]]:
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for message in messages:
            journal_date = self._journal_date_from_timestamp(message.get("timestamp"))
            grouped.setdefault(journal_date, []).append(message)
        return grouped

    def _get_journal_content_sync(self, memory_key: str, journal_date: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT content
                FROM {self.JOURNALS_TABLE}
                WHERE memory_key = ? AND journal_date = ?
                """,
                (memory_key, journal_date),
            ).fetchone()
        return str(row["content"]) if row is not None else ""

    def _upsert_journal_sync(self, memory_key: str, journal_date: str, content: str) -> int:
        now = time.time()
        with self._connect() as conn:
            existing = conn.execute(
                f"""
                SELECT id
                FROM {self.JOURNALS_TABLE}
                WHERE memory_key = ? AND journal_date = ?
                """,
                (memory_key, journal_date),
            ).fetchone()
            if existing is None:
                cursor = conn.execute(
                    f"""
                    INSERT INTO {self.JOURNALS_TABLE} (
                        memory_key,
                        journal_date,
                        content,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (memory_key, journal_date, content, now, now),
                )
                conn.commit()
                self.logger.debug(
                    "Inserted new journal row: memory_key=%s journal_date=%s row_id=%d content_len=%d",
                    memory_key,
                    journal_date,
                    int(cursor.lastrowid),
                    len(content),
                )
                return int(cursor.lastrowid)

            journal_id = int(existing["id"])
            conn.execute(
                f"""
                UPDATE {self.JOURNALS_TABLE}
                SET content = ?, updated_at = ?
                WHERE id = ?
                """,
                (content, now, journal_id),
            )
            conn.commit()
            self.logger.debug(
                "Updated journal row: memory_key=%s journal_date=%s row_id=%d content_len=%d",
                memory_key,
                journal_date,
                journal_id,
                len(content),
            )
            return journal_id

    def _get_journal_entry_by_date_sync(
        self,
        memory_key: str,
        journal_date: str,
    ) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT id, journal_date, content, updated_at
                FROM {self.JOURNALS_TABLE}
                WHERE memory_key = ? AND journal_date = ?
                """,
                (memory_key, journal_date),
            ).fetchone()

        if row is None:
            return None
        return self._row_to_memory_result(row, matched_keywords=[])

    def _get_latest_journal_entries_sync(self, memory_key: str, limit: int) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, journal_date, content, updated_at
                FROM {self.JOURNALS_TABLE}
                WHERE memory_key = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (memory_key, int(limit)),
            ).fetchall()
        self.logger.debug(
            "Fetched latest journal entries: memory_key=%s limit=%d rows=%d",
            memory_key,
            limit,
            len(rows),
        )
        return [self._row_to_memory_result(row, matched_keywords=[]) for row in rows]

    def _search_journals_sync(
        self,
        memory_key: str,
        keywords: List[str],
        limit: int,
        journal_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        normalized_keywords = self._normalize_keywords(keywords)
        if not normalized_keywords:
            self.logger.debug(
                "Journal search fallback to latest entries: memory_key=%s because normalized keywords are empty",
                memory_key,
            )
            return self._get_latest_journal_entries_sync(memory_key, limit)

        merged: Dict[int, Dict[str, Any]] = {}
        fts_keywords = [keyword for keyword in normalized_keywords if len(keyword) >= 3]
        self.logger.debug(
            "Journal search starting: memory_key=%s journal_date=%s keywords=%s fts_keywords=%s limit=%d",
            memory_key,
            journal_date,
            normalized_keywords,
            fts_keywords,
            limit,
        )
        if fts_keywords:
            fts_rows = self._fts_search_sync(memory_key, fts_keywords, limit, journal_date)
            self.logger.debug(
                "Journal FTS search results: memory_key=%s rows=%d",
                memory_key,
                len(fts_rows),
            )
            for row in fts_rows:
                matched_keywords = self._matched_keywords(row["content"], normalized_keywords)
                merged[int(row["id"])] = self._row_to_memory_result(
                    row,
                    matched_keywords=matched_keywords,
                    fts_rank=float(row["fts_rank"]) if row["fts_rank"] is not None else None,
                )

        needs_like_fallback = any(len(keyword) < 3 for keyword in normalized_keywords) or len(merged) < limit
        if needs_like_fallback:
            like_limit = max(limit * 2, limit)
            like_rows = self._like_search_sync(memory_key, normalized_keywords, like_limit, journal_date)
            self.logger.debug(
                "Journal LIKE fallback search: memory_key=%s rows=%d like_limit=%d needs_like_fallback=%s",
                memory_key,
                len(like_rows),
                like_limit,
                needs_like_fallback,
            )
            for row in like_rows:
                matched_keywords = self._matched_keywords(row["content"], normalized_keywords)
                if not matched_keywords:
                    continue

                journal_id = int(row["id"])
                if journal_id in merged:
                    existing_keywords = merged[journal_id]["metadata"].get("matched_keywords", [])
                    merged[journal_id]["metadata"]["matched_keywords"] = self._normalize_keywords(
                        existing_keywords + matched_keywords
                    )
                    continue

                merged[journal_id] = self._row_to_memory_result(
                    row,
                    matched_keywords=matched_keywords,
                    fts_rank=None,
                )

        results = list(merged.values())
        results.sort(
            key=lambda item: (
                -len(item["metadata"].get("matched_keywords", [])),
                item["_fts_rank"] if item["_fts_rank"] is not None else float("inf"),
                -float(item["metadata"].get("updated_at", 0.0)),
            )
        )

        trimmed: List[Dict[str, Any]] = []
        for item in results[:limit]:
            item.pop("_fts_rank", None)
            trimmed.append(item)
        self.logger.debug(
            "Journal search merged results: memory_key=%s total=%d returned=%d",
            memory_key,
            len(results),
            len(trimmed),
        )
        return trimmed

    def _fts_search_sync(
        self,
        memory_key: str,
        keywords: List[str],
        limit: int,
        journal_date: Optional[str],
    ) -> List[sqlite3.Row]:
        query = " OR ".join(self._quote_fts_term(keyword) for keyword in keywords)
        sql = f"""
            SELECT j.id, j.journal_date, j.content, j.updated_at, bm25({self.JOURNAL_FTS_TABLE}) AS fts_rank
            FROM {self.JOURNAL_FTS_TABLE}
            JOIN {self.JOURNALS_TABLE} j ON j.id = {self.JOURNAL_FTS_TABLE}.rowid
            WHERE j.memory_key = ?
              AND {self.JOURNAL_FTS_TABLE} MATCH ?
        """
        params: List[Any] = [memory_key, query]
        if journal_date:
            sql += " AND j.journal_date = ?"
            params.append(journal_date)
        sql += " ORDER BY fts_rank ASC, j.updated_at DESC LIMIT ?"
        params.append(int(limit))

        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        self.logger.debug(
            "Executed journal FTS query: memory_key=%s journal_date=%s query=%s rows=%d",
            memory_key,
            journal_date,
            query,
            len(rows),
        )
        return rows

    def _like_search_sync(
        self,
        memory_key: str,
        keywords: List[str],
        limit: int,
        journal_date: Optional[str],
    ) -> List[sqlite3.Row]:
        clauses = ["j.memory_key = ?"]
        params: List[Any] = [memory_key]
        if journal_date:
            clauses.append("j.journal_date = ?")
            params.append(journal_date)

        keyword_clauses: List[str] = []
        for keyword in keywords:
            keyword_clauses.append("LOWER(j.content) LIKE ?")
            params.append(f"%{keyword.casefold()}%")

        if keyword_clauses:
            clauses.append(f"({' OR '.join(keyword_clauses)})")

        sql = f"""
            SELECT j.id, j.journal_date, j.content, j.updated_at
            FROM {self.JOURNALS_TABLE} j
            WHERE {' AND '.join(clauses)}
            ORDER BY j.updated_at DESC
            LIMIT ?
        """
        params.append(int(limit))

        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        self.logger.debug(
            "Executed journal LIKE query: memory_key=%s journal_date=%s keywords=%s rows=%d",
            memory_key,
            journal_date,
            keywords,
            len(rows),
        )
        return rows

    def _clear_sync(self, memory_key: str) -> None:
        with self._connect() as conn:
            conn.execute(
                f"DELETE FROM {self.JOURNALS_TABLE} WHERE memory_key = ?",
                (memory_key,),
            )
            conn.execute(
                f"DELETE FROM {self.JOURNAL_STATE_TABLE} WHERE memory_key = ?",
                (memory_key,),
            )
            conn.commit()
        self.logger.debug("Cleared sqlite journal rows and state for %s", memory_key)

    def _delete_sync(self, journal_ids: List[int]) -> None:
        placeholders = ",".join("?" for _ in journal_ids)
        with self._connect() as conn:
            conn.execute(
                f"DELETE FROM {self.JOURNALS_TABLE} WHERE id IN ({placeholders})",
                tuple(journal_ids),
            )
            conn.commit()
        self.logger.debug("Deleted sqlite journal rows: ids=%s", journal_ids)

    def _messages_table_exists_sync(self) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name = ?
                """,
                (self.MESSAGES_TABLE,),
            ).fetchone()
        return row is not None

    def _extract_user_messages(self, messages: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            message
            for message in messages
            if str(message.get("role", "")).lower() == "user" and bool(message.get("content"))
        ]

    def _contains_explicit_memory_intent(self, messages: Sequence[Dict[str, Any]]) -> bool:
        for message in messages:
            content = str(message.get("content", ""))
            if any(pattern.search(content) for pattern in self._explicit_memory_patterns):
                self.logger.debug("Explicit journal trigger detected in user message: %s", self._truncate(content))
                return True
        return False

    def _should_extract(self, state: StreamMemoryState, unread_count: int) -> bool:
        if unread_count >= self.force_extraction_threshold:
            self.logger.debug(
                "Journal extraction forced: unread_count=%d force_threshold=%d",
                unread_count,
                self.force_extraction_threshold,
            )
            return True
        if unread_count < self.memory_threshold:
            self.logger.debug(
                "Journal extraction below threshold: unread_count=%d threshold=%d",
                unread_count,
                self.memory_threshold,
            )
            return False
        if state.last_written_at <= 0:
            self.logger.debug("Journal extraction allowed: no previous write timestamp")
            return True
        elapsed = time.time() - state.last_written_at
        decision = elapsed >= self.memory_interval_seconds
        self.logger.debug(
            "Journal extraction interval check: elapsed=%.3f interval_seconds=%d decision=%s",
            elapsed,
            self.memory_interval_seconds,
            decision,
        )
        return decision

    def _format_messages_for_storage(
        self,
        *,
        memory_key: str,
        journal_date: str,
        messages: List[Dict[str, Any]],
    ) -> str:
        lines: List[str] = []
        for msg in messages:
            if not self._is_journal_candidate_message(msg):
                continue

            role = str(msg.get("role", "unknown")).title()
            sender_id = self._display_sender_id(
                memory_key=memory_key,
                journal_date=journal_date,
                role=str(msg.get("role", "")),
                sender_id=msg.get("sender_id"),
            )
            content = str(msg.get("content", "")).strip()
            if not content:
                continue

            prefix_parts: List[str] = []
            timestamp = self._format_timestamp(msg.get("timestamp"))
            if timestamp:
                prefix_parts.append(f"[{timestamp}]")
            prefix_parts.append(role)
            if sender_id:
                prefix_parts.append(str(sender_id))
            lines.append(f"{' '.join(prefix_parts)}: {content}")
        return "\n\n".join(lines)

    def _is_journal_candidate_message(self, message: Dict[str, Any]) -> bool:
        role = str(message.get("role", "")).lower()
        message_type = str(message.get("type", "")).lower()
        if message_type in {
            MessageType.FUNCTION_CALL.value,
            MessageType.FUNCTION_CALL_OUTPUT.value,
        }:
            return False
        return role in {"user", "assistant"} and bool(str(message.get("content", "")).strip())

    @staticmethod
    def _row_to_memory_result(
        row: sqlite3.Row,
        matched_keywords: List[str],
        fts_rank: Optional[float] = None,
    ) -> Dict[str, Any]:
        return {
            "id": str(row["id"]),
            "content": str(row["content"]),
            "metadata": {
                "journal_date": str(row["journal_date"]),
                "updated_at": float(row["updated_at"]),
                "matched_keywords": matched_keywords,
            },
            "_fts_rank": fts_rank,
        }

    @staticmethod
    def _normalise_text(content: str) -> str:
        lines: List[str] = []
        previous_blank = False
        for raw_line in str(content or "").splitlines():
            normalized_line = " ".join(raw_line.split()).strip()
            if not normalized_line:
                if lines and not previous_blank:
                    lines.append("")
                previous_blank = True
                continue
            lines.append(normalized_line)
            previous_blank = False

        while lines and lines[0] == "":
            lines.pop(0)
        while lines and lines[-1] == "":
            lines.pop()
        return "\n".join(lines)

    @staticmethod
    def _normalize_keywords(keywords: Sequence[str]) -> List[str]:
        unique: List[str] = []
        seen: set[str] = set()
        for keyword in keywords:
            item = " ".join(str(keyword or "").split()).strip()
            if not item:
                continue
            key = item.casefold()
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique

    @staticmethod
    def _matched_keywords(content: str, keywords: Sequence[str]) -> List[str]:
        haystack = str(content or "").casefold()
        matches: List[str] = []
        seen: set[str] = set()
        for keyword in keywords:
            item = " ".join(str(keyword or "").split()).strip()
            if not item:
                continue
            folded = item.casefold()
            if folded in seen or folded not in haystack:
                continue
            seen.add(folded)
            matches.append(item)
        return matches

    @staticmethod
    def _extract_storage_path(message_storage) -> Optional[Path]:
        raw_path = getattr(message_storage, "path", None)
        if raw_path is None:
            return None
        return Path(str(raw_path)).expanduser().resolve()

    @staticmethod
    def _current_local_date() -> str:
        return datetime.now().astimezone().strftime("%Y-%m-%d")

    @staticmethod
    def _journal_date_from_timestamp(timestamp: Any) -> str:
        try:
            return datetime.fromtimestamp(float(timestamp)).astimezone().strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError):
            return MemoryStorageBasic._current_local_date()

    def _display_sender_id(
        self,
        *,
        memory_key: str,
        journal_date: str,
        role: str,
        sender_id: Any,
    ) -> Optional[str]:
        sender = str(sender_id or "").strip()
        if not sender:
            return None
        if str(role).lower() != "user" or self._is_readable_sender_id(sender):
            return sender

        alias_key = (memory_key, journal_date)
        alias_map = self._speaker_aliases.setdefault(alias_key, {})
        if sender in alias_map:
            return alias_map[sender]

        alias = f"用户{self._alias_label(len(alias_map))}"
        alias_map[sender] = alias
        self.logger.debug(
            "Assigned normalized sender alias: memory_key=%s journal_date=%s raw_sender=%s alias=%s",
            memory_key,
            journal_date,
            sender,
            alias,
        )
        return alias

    @staticmethod
    def _is_readable_sender_id(sender_id: str) -> bool:
        sender = str(sender_id or "").strip()
        if not sender:
            return False
        if len(sender) > 24 and re.fullmatch(r"[a-f0-9-]{24,}", sender, re.IGNORECASE):
            return False
        if re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            sender,
            re.IGNORECASE,
        ):
            return False
        if len(sender) >= 18 and re.fullmatch(r"[A-Za-z0-9_-]{18,}", sender):
            digit_count = sum(char.isdigit() for char in sender)
            separator_count = sum(char in "_-" for char in sender)
            uppercase_count = sum(char.isupper() for char in sender)
            if digit_count >= 4 or separator_count >= 2 or uppercase_count >= 4:
                return False
        return True

    @staticmethod
    def _alias_label(index: int) -> str:
        alphabet = string.ascii_uppercase
        value = index
        label = ""
        while True:
            value, remainder = divmod(value, len(alphabet))
            label = alphabet[remainder] + label
            if value == 0:
                break
            value -= 1
        return label

    @staticmethod
    def _normalize_journal_date(journal_date: Optional[str]) -> Optional[str]:
        if journal_date is None:
            return None
        normalized = str(journal_date).strip()
        if not normalized:
            return None
        datetime.strptime(normalized, "%Y-%m-%d")
        return normalized

    @staticmethod
    def _format_timestamp(timestamp: Any) -> str:
        if timestamp is None:
            return ""
        try:
            dt = datetime.fromtimestamp(float(timestamp)).astimezone()
        except (TypeError, ValueError, OSError):
            return ""
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _quote_fts_term(keyword: str) -> str:
        return '"' + str(keyword).replace('"', '""') + '"'

    @staticmethod
    def _truncate(value: str, limit: int = 120) -> str:
        text = str(value or "")
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."

    @classmethod
    def _summarize_results_for_log(cls, results: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        summary: List[Dict[str, Any]] = []
        for item in results:
            metadata = item.get("metadata", {}) if isinstance(item, dict) else {}
            summary.append(
                {
                    "id": item.get("id") if isinstance(item, dict) else None,
                    "journal_date": metadata.get("journal_date"),
                    "matched_keywords": metadata.get("matched_keywords"),
                    "content": cls._truncate(item.get("content", "") if isinstance(item, dict) else str(item), limit=10000),
                }
            )
        return summary

    def _stream_lock(self, memory_key: str) -> asyncio.Lock:
        return self._stream_locks.setdefault(memory_key, asyncio.Lock())
