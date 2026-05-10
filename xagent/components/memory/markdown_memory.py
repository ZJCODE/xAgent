"""Markdown-based memory storage using plain files."""

import asyncio
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# Allowed scope values for search/list
_VALID_SCOPES = {"daily", "weekly", "monthly", "yearly", "all"}


class MarkdownMemory:
    """File-based memory organized as daily/weekly/monthly/yearly markdown files.

    Common file reads, writes, and directory scans use ``pathlib`` through
    ``asyncio.to_thread``. Keyword search keeps ``grep`` for efficient recursive
    search and falls back to a Python scanner when unavailable.
    """

    def __init__(self, memory_dir: str) -> None:
        self.root = Path(memory_dir)
        self._write_lock = asyncio.Lock()
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
        heading.
        """
        d = target_date or date.today()
        path = self.daily_path(d)

        # Ensure parent directory exists
        await self._mkdir(path.parent)

        now = datetime.now()
        timestamp_heading = f"## {now.hour:02d}:{now.minute:02d}"
        block = f"\n---\n\n{timestamp_heading}\n\n{content.rstrip()}\n"

        async with self._write_lock:
            await self._append_file(path, block)
        logger.debug("Appended daily entry: %s (%d chars)", path, len(content))
        return path

    # ------------------------------------------------------------------
    # Core read helpers
    # ------------------------------------------------------------------

    async def read_file(self, path: Path) -> str:
        """Read a single markdown file."""
        return await asyncio.to_thread(self._read_text_sync, path)

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
        async with self._write_lock:
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
        context_lines = max(0, min(int(context_lines), 20))
        search_dir = str(self.root / scope) if scope != "all" else str(self.root)

        if not query:
            return ""

        try:
            proc = await asyncio.create_subprocess_exec(
                "grep", "-Frni",
                "--include=*.md",
                f"-C{context_lines}",
                "--",
                query,
                search_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode in (0, 1):
                return stdout.decode(errors="replace")
        except (FileNotFoundError, OSError):
            pass

        return await asyncio.to_thread(
            self._search_keyword_sync,
            query,
            Path(search_dir),
            context_lines,
        )

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
        """List markdown files in a scope directory."""
        scope = scope if scope in _VALID_SCOPES else "all"
        search_dir = self.root / scope if scope != "all" else self.root
        return await asyncio.to_thread(self._list_files_sync, search_dir)

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
        await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)

    @staticmethod
    async def _append_file(path: Path, content: str) -> None:
        """Append *content* to *path*."""
        await asyncio.to_thread(MarkdownMemory._append_file_sync, path, content)

    @staticmethod
    async def _write_file(path: Path, content: str) -> None:
        """Overwrite *path*."""
        await asyncio.to_thread(MarkdownMemory._write_file_sync, path, content)

    @staticmethod
    def _read_text_sync(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ""

    @staticmethod
    def _append_file_sync(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(content)

    @staticmethod
    def _write_file_sync(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    @staticmethod
    def _list_files_sync(search_dir: Path) -> List[str]:
        if not search_dir.exists():
            return []
        return sorted(str(path) for path in search_dir.rglob("*.md") if path.is_file())

    @staticmethod
    def _search_keyword_sync(query: str, search_dir: Path, context_lines: int) -> str:
        if not search_dir.exists():
            return ""

        needle = query.casefold()
        blocks: list[str] = []
        for path in sorted(search_dir.rglob("*.md")):
            if not path.is_file():
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for index, line in enumerate(lines):
                if needle not in line.casefold():
                    continue
                start = max(0, index - context_lines)
                end = min(len(lines), index + context_lines + 1)
                blocks.extend(
                    f"{path}:{line_number + 1}:{lines[line_number]}"
                    for line_number in range(start, end)
                )
                blocks.append("--")

        if blocks and blocks[-1] == "--":
            blocks.pop()
        return "\n".join(blocks)
