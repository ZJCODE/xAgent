"""Markdown-based memory storage using plain files and basic shell commands."""

import asyncio
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# Allowed scope values for search/list
_VALID_SCOPES = {"daily", "weekly", "monthly", "yearly", "all"}


class MarkdownMemory:
    """File-based memory organized as daily/weekly/monthly/yearly markdown files.

    All read/write operations use ``asyncio.create_subprocess_exec`` with basic
    POSIX commands (``cat``, ``grep``, ``find``, ``mkdir``, ``tee``).
    Content is written via **stdin pipe** — never interpolated into shell
    arguments — to prevent command-injection.
    """

    def __init__(self, memory_dir: str) -> None:
        self.root = Path(memory_dir)
        self._ensure_dirs_sync()

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _daily_dir(self, d: date) -> Path:
        return self.root / "daily" / str(d.year) / f"{d.year}-{d.month:02d}"

    def daily_path(self, d: date) -> Path:
        return self._daily_dir(d) / f"{d.isoformat()}.md"

    def weekly_path(self, week_start: date, week_end: date) -> Path:
        return (
            self.root
            / "weekly"
            / str(week_start.year)
            / f"{week_start.isoformat()}_to_{week_end.isoformat()}.md"
        )

    def monthly_path(self, year: int, month: int) -> Path:
        return self.root / "monthly" / str(year) / f"{year}-{month:02d}.md"

    def yearly_path(self, year: int) -> Path:
        return self.root / "yearly" / f"{year}.md"

    # ------------------------------------------------------------------
    # Directory bootstrap (sync, called once in __init__)
    # ------------------------------------------------------------------

    def _ensure_dirs_sync(self) -> None:
        for sub in ("daily", "weekly", "monthly", "yearly"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Core write: append to daily
    # ------------------------------------------------------------------

    async def append_daily(self, content: str, target_date: Optional[date] = None) -> Path:
        """Append a diary entry to the daily markdown file.

        Each entry is separated by ``---`` and starts with a ``## HH:MM``
        heading.  Content is piped via stdin to ``tee -a``.
        """
        d = target_date or date.today()
        path = self.daily_path(d)

        # Ensure parent directory exists
        await self._mkdir(path.parent)

        now = datetime.now()
        timestamp_heading = f"## {now.hour:02d}:{now.minute:02d}"
        block = f"\n---\n\n{timestamp_heading}\n\n{content.rstrip()}\n"

        await self._append_file(path, block)
        logger.debug("Appended daily entry: %s (%d chars)", path, len(content))
        return path

    # ------------------------------------------------------------------
    # Core read helpers
    # ------------------------------------------------------------------

    async def read_file(self, path: Path) -> str:
        """Read a single markdown file via ``cat``."""
        if not path.exists():
            return ""
        proc = await asyncio.create_subprocess_exec(
            "cat", str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode(errors="replace")

    async def read_recent_dailies(self, days: int = 3) -> List[Tuple[str, str]]:
        """Return ``[(date_str, content), ...]`` for the last *days* days."""
        today = date.today()
        results: List[Tuple[str, str]] = []
        for offset in range(days):
            d = today - timedelta(days=offset)
            path = self.daily_path(d)
            text = await self.read_file(path)
            if text.strip():
                results.append((d.isoformat(), text))
        return results

    # ------------------------------------------------------------------
    # Summary write (overwrite)
    # ------------------------------------------------------------------

    async def write_summary(self, path: Path, content: str) -> Path:
        """Write (overwrite) a summary file (weekly / monthly / yearly)."""
        await self._mkdir(path.parent)
        await self._write_file(path, content)
        logger.debug("Wrote summary: %s (%d chars)", path, len(content))
        return path

    # ------------------------------------------------------------------
    # Search: keyword grep
    # ------------------------------------------------------------------

    async def search_keyword(
        self,
        query: str,
        scope: str = "all",
        context_lines: int = 3,
    ) -> str:
        """Search markdown files via ``grep -rni`` with context lines.

        Returns the raw grep output as a string (file paths + matches).
        """
        scope = scope if scope in _VALID_SCOPES else "all"
        search_dir = str(self.root / scope) if scope != "all" else str(self.root)

        # Sanitise query for use as grep pattern (escape regex metacharacters
        # so the search is a plain-string match).
        safe_query = re.escape(query)

        proc = await asyncio.create_subprocess_exec(
            "grep", "-rni",
            f"--include=*.md",
            f"-C{context_lines}",
            safe_query,
            search_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode(errors="replace")

    # ------------------------------------------------------------------
    # Search: date range (find + cat)
    # ------------------------------------------------------------------

    async def search_date_range(
        self,
        start: str,
        end: Optional[str] = None,
        scope: str = "daily",
    ) -> str:
        """Read all daily files within a date range and concatenate them.

        *start* and *end* are ``YYYY-MM-DD`` strings.  When *end* is ``None``
        only the single date is read.
        """
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end) if end else start_date

        if end_date < start_date:
            start_date, end_date = end_date, start_date

        parts: List[str] = []
        current = start_date
        while current <= end_date:
            path = self.daily_path(current)
            text = await self.read_file(path)
            if text.strip():
                parts.append(f"# {current.isoformat()}\n\n{text}")
            current += timedelta(days=1)

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # List files
    # ------------------------------------------------------------------

    async def list_files(self, scope: str = "all") -> List[str]:
        """List markdown files in a scope directory via ``find``."""
        scope = scope if scope in _VALID_SCOPES else "all"
        search_dir = str(self.root / scope) if scope != "all" else str(self.root)

        proc = await asyncio.create_subprocess_exec(
            "find", search_dir,
            "-name", "*.md",
            "-type", "f",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        lines = stdout.decode(errors="replace").strip().splitlines()
        return sorted(line.strip() for line in lines if line.strip())

    # ------------------------------------------------------------------
    # Week helpers (ISO week: Monday–Sunday)
    # ------------------------------------------------------------------

    @staticmethod
    def current_week_range() -> Tuple[date, date]:
        """Return (monday, sunday) of the current ISO week."""
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        sunday = monday + timedelta(days=6)
        return monday, sunday

    @staticmethod
    def week_range_for(d: date) -> Tuple[date, date]:
        """Return (monday, sunday) of the ISO week containing *d*."""
        monday = d - timedelta(days=d.weekday())
        sunday = monday + timedelta(days=6)
        return monday, sunday

    # ------------------------------------------------------------------
    # Internal I/O primitives (stdin-pipe based for safety)
    # ------------------------------------------------------------------

    @staticmethod
    async def _mkdir(path: Path) -> None:
        await asyncio.create_subprocess_exec(
            "mkdir", "-p", str(path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    @staticmethod
    async def _append_file(path: Path, content: str) -> None:
        """Append *content* to *path* via ``tee -a`` with stdin pipe."""
        proc = await asyncio.create_subprocess_exec(
            "tee", "-a", str(path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate(input=content.encode())

    @staticmethod
    async def _write_file(path: Path, content: str) -> None:
        """Overwrite *path* via ``tee`` with stdin pipe."""
        proc = await asyncio.create_subprocess_exec(
            "tee", str(path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate(input=content.encode())
