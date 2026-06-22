"""Markdown-file store for long-term diary memory."""

import asyncio
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Literal, Optional, Tuple, cast

logger = logging.getLogger(__name__)

MemoryScope = Literal["daily", "weekly", "monthly", "yearly", "all"]

_TIME_SCOPES: tuple[str, ...] = ("daily", "weekly", "monthly", "yearly")
_VALID_SCOPES: set[str] = {*_TIME_SCOPES, "all"}


class MarkdownMemoryStore:
    """Store diary memory as daily, weekly, monthly, and yearly markdown files.

    This class owns file layout and I/O only. Scheduling writes, generating
    summaries, and deciding what should be remembered live in higher layers.
    """

    def __init__(self, memory_dir: str) -> None:
        self.root = Path(memory_dir).expanduser()
        self._write_lock = asyncio.Lock()
        self._ensure_dirs_sync()

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _daily_dir(self, target_date: date) -> Path:
        return self.root / "daily" / str(target_date.year) / f"{target_date.year}-{target_date.month:02d}"

    def daily_path(self, target_date: date) -> Path:
        return self._daily_dir(target_date) / f"{target_date.isoformat()}.md"

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
        for sub in _TIME_SCOPES:
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Core write: append to daily
    # ------------------------------------------------------------------

    async def append_daily(self, content: str, target_date: Optional[date] = None) -> Path:
        """Append a diary entry to the daily markdown file.

        Each entry is separated by ``---`` and starts with a ``## HH:MM``
        heading.
        """
        entry_date = target_date or date.today()
        path = self.daily_path(entry_date)

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
            entry_date = today - timedelta(days=offset)
            path = self.daily_path(entry_date)
            text = await self.read_file(path)
            if text.strip():
                results.append((entry_date.isoformat(), text))
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
        scope: MemoryScope | str = "all",
        context_lines: int = 3,
    ) -> str:
        """Search markdown files via ``grep -rni`` with context lines.

        Returns the raw grep output as a string (file paths + matches).
        """
        scope = self._normalize_scope(scope)
        context_lines = max(0, min(int(context_lines), 20))

        if not query:
            return ""

        if scope == "all":
            return await asyncio.to_thread(
                self._search_keyword_many_sync,
                query,
                self._scope_roots(scope),
                context_lines,
            )

        search_dir = self._scope_root(scope)

        try:
            proc = await asyncio.create_subprocess_exec(
                "grep", "-Frni",
                "--include=*.md",
                f"-C{context_lines}",
                "--",
                query,
                str(search_dir),
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
            search_dir,
            context_lines,
        )

    # ------------------------------------------------------------------
    # Search: date range (find + cat)
    # ------------------------------------------------------------------

    async def search_date_range(
        self,
        start: str,
        end: Optional[str] = None,
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

    async def list_files(self, scope: MemoryScope | str = "all") -> List[str]:
        """List markdown files in a scope directory."""
        scope = self._normalize_scope(scope)
        return await asyncio.to_thread(self._list_files_many_sync, self._scope_roots(scope))

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
    def week_range_for(target_date: date) -> Tuple[date, date]:
        """Return (monday, sunday) of the ISO week containing *target_date*."""
        monday = target_date - timedelta(days=target_date.weekday())
        sunday = monday + timedelta(days=6)
        return monday, sunday

    # ------------------------------------------------------------------
    # Scope helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_scope(scope: MemoryScope | str) -> MemoryScope:
        if scope in _VALID_SCOPES:
            return cast(MemoryScope, scope)
        return "all"

    def _scope_root(self, scope: MemoryScope | str) -> Path:
        normalized_scope = self._normalize_scope(scope)
        return self.root if normalized_scope == "all" else self.root / normalized_scope

    def _scope_roots(self, scope: MemoryScope | str) -> List[Path]:
        normalized_scope = self._normalize_scope(scope)
        if normalized_scope == "all":
            return [self.root / scope_name for scope_name in _TIME_SCOPES]
        return [self.root / normalized_scope]

    # ------------------------------------------------------------------
    # Internal I/O primitives (stdin-pipe based for safety)
    # ------------------------------------------------------------------

    @staticmethod
    async def _mkdir(path: Path) -> None:
        await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)

    @staticmethod
    async def _append_file(path: Path, content: str) -> None:
        """Append *content* to *path*."""
        await asyncio.to_thread(MarkdownMemoryStore._append_file_sync, path, content)

    @staticmethod
    async def _write_file(path: Path, content: str) -> None:
        """Overwrite *path*."""
        await asyncio.to_thread(MarkdownMemoryStore._write_file_sync, path, content)

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
    def _list_files_many_sync(search_dirs: List[Path]) -> List[str]:
        files: list[str] = []
        for search_dir in search_dirs:
            files.extend(MarkdownMemoryStore._list_files_sync(search_dir))
        return sorted(files)

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

    @staticmethod
    def _search_keyword_many_sync(query: str, search_dirs: List[Path], context_lines: int) -> str:
        blocks: list[str] = []
        for search_dir in search_dirs:
            text = MarkdownMemoryStore._search_keyword_sync(query, search_dir, context_lines)
            if text.strip():
                blocks.append(text)
        return "\n--\n".join(blocks)
