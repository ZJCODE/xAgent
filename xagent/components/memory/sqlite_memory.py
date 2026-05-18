"""SQLite-backed long-term memory storage."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, List, Optional, Tuple

from ..sqlite_query import execute_readonly_query

logger = logging.getLogger(__name__)


class SQLiteMemoryConfig:
    """Configuration constants for ``SQLiteMemory``."""

    DEFAULT_PATH = "~/.xagent/memory/memory.sqlite3"
    CONNECT_TIMEOUT = 5.0
    MEMORY_DB_FILENAME = "memory.sqlite3"
    CURRENT_TABLES = {"memory_entries", "memory_summaries", "people_facts"}


class SQLiteMemory:
    """Persistent long-term memory backed by a dedicated SQLite database."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = Path(path or SQLiteMemoryConfig.DEFAULT_PATH).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger(self.__class__.__name__)
        self._write_lock = asyncio.Lock()
        self._initialize_database()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path),
            timeout=SQLiteMemoryConfig.CONNECT_TIMEOUT,
        )
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            existing_tables = {
                row["name"]
                for row in connection.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table'
                    AND name NOT LIKE 'sqlite_%'
                    """
                ).fetchall()
            }
            unexpected_tables = existing_tables - SQLiteMemoryConfig.CURRENT_TABLES
            if unexpected_tables:
                self.logger.warning(
                    "Unexpected memory schema at %s; recreating memory database tables.",
                    self.path,
                )
                for table_name in sorted(existing_tables):
                    connection.execute(f"DROP TABLE IF EXISTS {table_name}")

            self._create_current_schema(connection)
            connection.commit()

    def _create_current_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_date TEXT NOT NULL,
                created_at REAL NOT NULL,
                source TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                period_type TEXT NOT NULL,
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                generated_at REAL NOT NULL,
                content TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(period_type, period_start, period_end)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS people_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_key TEXT NOT NULL,
                display_name TEXT NOT NULL,
                fact TEXT NOT NULL,
                evidence TEXT NOT NULL,
                source TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                created_at REAL NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(person_key, fact, evidence)
            )
            """
        )
        self._ensure_current_indexes(connection)

    def _ensure_current_indexes(self, connection: sqlite3.Connection) -> None:
        connection.execute("CREATE INDEX IF NOT EXISTS idx_memory_entries_date ON memory_entries(entry_date)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_memory_entries_created_at ON memory_entries(created_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_memory_entries_source ON memory_entries(source)")
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memory_summaries_period
            ON memory_summaries(period_type, period_start, period_end)
            """
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_people_facts_person ON people_facts(person_key)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_people_facts_observed_at ON people_facts(observed_at)")

    async def add_entry(
        self,
        content: str,
        *,
        target_date: Optional[date] = None,
        source: str = "auto_diary",
        metadata: Optional[dict[str, Any]] = None,
    ) -> int:
        """Append one long-term memory entry and return its row id."""
        content = str(content or "").strip()
        if not content:
            return 0

        entry_date = (target_date or date.today()).isoformat()
        created_at = time.time()
        metadata_json = self._json_dumps(metadata or {})
        async with self._write_lock:
            return await asyncio.to_thread(
                self._add_entry_sync,
                entry_date,
                created_at,
                source,
                content,
                metadata_json,
            )

    def _add_entry_sync(
        self,
        entry_date: str,
        created_at: float,
        source: str,
        content: str,
        metadata_json: str,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO memory_entries (entry_date, created_at, source, content, metadata_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (entry_date, created_at, source, content, metadata_json),
            )
            connection.commit()
            return int(cursor.lastrowid or 0)

    async def read_recent_entries(self, days: int = 3) -> List[Tuple[str, str]]:
        """Return grouped memory entry text for the last *days* days."""
        today = date.today()
        start = today - timedelta(days=max(1, int(days)) - 1)
        rows = await asyncio.to_thread(
            self._select_entries_sync,
            start.isoformat(),
            today.isoformat(),
        )
        return self._group_entries_by_date(rows)

    async def search_date_range(self, start: str, end: Optional[str] = None) -> str:
        """Return formatted memory entries for a date or inclusive date range."""
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end) if end else start_date
        if end_date < start_date:
            start_date, end_date = end_date, start_date

        rows = await asyncio.to_thread(
            self._select_entries_sync,
            start_date.isoformat(),
            end_date.isoformat(),
        )
        return self._format_entry_rows(rows)

    def _select_entries_sync(self, start: str, end: str) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT id, entry_date, created_at, source, content, metadata_json
                FROM memory_entries
                WHERE entry_date BETWEEN ? AND ?
                ORDER BY entry_date ASC, id ASC
                """,
                (start, end),
            ).fetchall()

    async def upsert_summary(
        self,
        *,
        period_type: str,
        period_start: date,
        period_end: date,
        content: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> int:
        """Insert or replace a periodic summary and return its row id."""
        content = str(content or "").strip()
        if not content:
            return 0
        async with self._write_lock:
            return await asyncio.to_thread(
                self._upsert_summary_sync,
                period_type,
                period_start.isoformat(),
                period_end.isoformat(),
                time.time(),
                content,
                self._json_dumps(metadata or {}),
            )

    def _upsert_summary_sync(
        self,
        period_type: str,
        period_start: str,
        period_end: str,
        generated_at: float,
        content: str,
        metadata_json: str,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO memory_summaries (
                    period_type, period_start, period_end, generated_at, content, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(period_type, period_start, period_end) DO UPDATE SET
                    generated_at = excluded.generated_at,
                    content = excluded.content,
                    metadata_json = excluded.metadata_json
                """,
                (period_type, period_start, period_end, generated_at, content, metadata_json),
            )
            connection.commit()
            if cursor.lastrowid:
                return int(cursor.lastrowid)
            row = connection.execute(
                """
                SELECT id
                FROM memory_summaries
                WHERE period_type = ? AND period_start = ? AND period_end = ?
                """,
                (period_type, period_start, period_end),
            ).fetchone()
            return int(row["id"]) if row is not None else 0

    async def summary_exists(
        self,
        *,
        period_type: str,
        period_start: date,
        period_end: date,
    ) -> bool:
        return await asyncio.to_thread(
            self._summary_exists_sync,
            period_type,
            period_start.isoformat(),
            period_end.isoformat(),
        )

    def _summary_exists_sync(self, period_type: str, period_start: str, period_end: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM memory_summaries
                WHERE period_type = ? AND period_start = ? AND period_end = ?
                LIMIT 1
                """,
                (period_type, period_start, period_end),
            ).fetchone()
        return row is not None

    async def read_summaries(
        self,
        *,
        period_type: str,
        period_start: date,
        period_end: date,
    ) -> str:
        """Return formatted summaries fully contained in a date range."""
        rows = await asyncio.to_thread(
            self._read_summaries_sync,
            period_type,
            period_start.isoformat(),
            period_end.isoformat(),
        )
        return "\n\n".join(
            f"# {row['period_type']} {row['period_start']} to {row['period_end']}\n\n{row['content']}"
            for row in rows
            if str(row["content"]).strip()
        )

    def _read_summaries_sync(
        self,
        period_type: str,
        period_start: str,
        period_end: str,
    ) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT period_type, period_start, period_end, content
                FROM memory_summaries
                WHERE period_type = ?
                AND period_start >= ?
                AND period_end <= ?
                ORDER BY period_start ASC, period_end ASC
                """,
                (period_type, period_start, period_end),
            ).fetchall()

    async def add_people_facts(
        self,
        person_key: str,
        facts: list[dict[str, Any]],
        *,
        display_name: Optional[str] = None,
        observed_at: Optional[date] = None,
    ) -> int:
        """Append quote-backed person facts, deduplicating by person/fact/evidence."""
        normalized_facts = self._normalize_people_facts(facts)
        if not normalized_facts:
            return 0
        observed = (observed_at or date.today()).isoformat()
        created_at = time.time()
        rows = [
            (
                person_key,
                str(fact.get("display_name") or display_name or person_key).strip(),
                fact["fact"],
                fact["evidence"],
                str(fact.get("source") or "").strip(),
                observed,
                created_at,
                self._json_dumps(fact.get("metadata") or {}),
            )
            for fact in normalized_facts
        ]
        async with self._write_lock:
            return await asyncio.to_thread(self._add_people_facts_sync, rows)

    def _add_people_facts_sync(self, rows: list[tuple]) -> int:
        with self._connect() as connection:
            before = connection.total_changes
            connection.executemany(
                """
                INSERT OR IGNORE INTO people_facts (
                    person_key, display_name, fact, evidence, source,
                    observed_at, created_at, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            connection.commit()
            return int(connection.total_changes - before)

    async def clear(self) -> None:
        """Clear all long-term memory rows without deleting the database file."""
        async with self._write_lock:
            await asyncio.to_thread(self._clear_sync)

    def _clear_sync(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM people_facts")
            connection.execute("DELETE FROM memory_summaries")
            connection.execute("DELETE FROM memory_entries")
            connection.commit()

    async def get_stats(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_stats_sync)

    def _get_stats_sync(self) -> dict[str, Any]:
        with self._connect() as connection:
            stats = {
                "path": str(self.path),
                "entries": self._count_table(connection, "memory_entries"),
                "summaries": self._count_table(connection, "memory_summaries"),
                "people_facts": self._count_table(connection, "people_facts"),
            }
            row = connection.execute(
                """
                SELECT MIN(entry_date) AS earliest_date, MAX(entry_date) AS latest_date
                FROM memory_entries
                """
            ).fetchone()
            if row is not None:
                stats["earliest_date"] = row["earliest_date"]
                stats["latest_date"] = row["latest_date"]
            return stats

    async def query_sql(self, sql: str, max_rows: int = 50) -> dict[str, Any]:
        return await asyncio.to_thread(
            execute_readonly_query,
            self.path,
            sql,
            max_rows=max_rows,
        )

    def get_storage_info(self) -> dict[str, str]:
        return {
            "stream": "memory",
            "backend": "sqlite",
            "path": str(self.path),
        }

    @staticmethod
    def current_week_range() -> Tuple[date, date]:
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        sunday = monday + timedelta(days=6)
        return monday, sunday

    @staticmethod
    def week_range_for(target_date: date) -> Tuple[date, date]:
        monday = target_date - timedelta(days=target_date.weekday())
        sunday = monday + timedelta(days=6)
        return monday, sunday

    @staticmethod
    def _count_table(connection: sqlite3.Connection, table_name: str) -> int:
        row = connection.execute(f"SELECT COUNT(*) AS row_count FROM {table_name}").fetchone()
        return int(row["row_count"]) if row is not None else 0

    @staticmethod
    def _group_entries_by_date(rows: list[sqlite3.Row]) -> List[Tuple[str, str]]:
        grouped: dict[str, list[str]] = {}
        for row in rows:
            grouped.setdefault(row["entry_date"], []).append(SQLiteMemory._format_single_entry(row))
        return [
            (entry_date, "\n\n".join(parts))
            for entry_date, parts in grouped.items()
            if any(part.strip() for part in parts)
        ]

    @staticmethod
    def _format_entry_rows(rows: list[sqlite3.Row]) -> str:
        grouped = SQLiteMemory._group_entries_by_date(rows)
        return "\n\n".join(
            f"# {entry_date}\n\n{content}"
            for entry_date, content in grouped
            if content.strip()
        )

    @staticmethod
    def _format_single_entry(row: sqlite3.Row) -> str:
        timestamp = datetime.fromtimestamp(float(row["created_at"])).strftime("%H:%M")
        source = str(row["source"] or "memory")
        return f"## {timestamp} [{source}]\n\n{str(row['content']).strip()}"

    @staticmethod
    def _normalize_people_facts(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in facts:
            if not isinstance(item, dict):
                continue
            fact = str(item.get("fact") or "").strip()
            evidence = str(item.get("evidence") or "").strip()
            if not fact or not evidence:
                continue
            normalized.append({
                **item,
                "fact": fact,
                "evidence": evidence,
            })
        return normalized

    @staticmethod
    def _json_dumps(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
