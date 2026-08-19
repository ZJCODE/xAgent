"""Markdown-file store for long-term diary memory."""

import asyncio
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Literal, Optional, Tuple, cast

from xagent.utils.search_terms import normalize_terms, score_text

logger = logging.getLogger(__name__)

MemoryScope = Literal["daily", "weekly", "monthly", "yearly", "notes", "all"]

_TIME_SCOPES: tuple[str, ...] = ("daily", "weekly", "monthly", "yearly")
_NOTE_SCOPE = "notes"
_SEARCH_SCOPES: tuple[str, ...] = (*_TIME_SCOPES, _NOTE_SCOPE)
_VALID_SCOPES: set[str] = {*_SEARCH_SCOPES, "all"}
_DEFAULT_SEARCH_MAX_RESULTS = 20
_DEFAULT_SEARCH_MAX_CHARS = 6000


class MarkdownMemory:
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

        Each entry starts with a ``## YYYY-MM-DD HH:MM`` heading.
        """
        entry_date = target_date or date.today()
        path = self.daily_path(entry_date)

        # Ensure parent directory exists
        await self._mkdir(path.parent)

        now = datetime.now()
        timestamp_heading = f"## {entry_date.isoformat()} {now.hour:02d}:{now.minute:02d}"
        block = f"\n{timestamp_heading}\n\n{content.rstrip()}\n"

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
        """Return ``[(date_str, content), ...]`` for the last *days* days,
        in chronological order (oldest first)."""
        today = date.today()
        results: List[Tuple[str, str]] = []
        for offset in range(days):
            entry_date = today - timedelta(days=offset)
            path = self.daily_path(entry_date)
            text = await self.read_file(path)
            if text.strip():
                results.append((entry_date.isoformat(), text))
        results.reverse()
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
    # Search: keyword (verbatim terms, labeled plain-text blocks)
    # ------------------------------------------------------------------

    async def search_keyword(
        self,
        terms: list[str],
        scope: MemoryScope | str = "all",
        context_lines: int = 3,
        max_results: int = _DEFAULT_SEARCH_MAX_RESULTS,
        max_chars: int = _DEFAULT_SEARCH_MAX_CHARS,
    ) -> str:
        """Search markdown files for verbatim terms (OR + hit-count ranking).

        Returns labeled plain-text blocks without filesystem paths or line numbers.
        """
        terms = normalize_terms(terms)
        scope = self._normalize_scope(scope)
        context_lines = max(0, min(int(context_lines), 20))
        max_results = max(1, int(max_results))
        max_chars = max(1, int(max_chars))

        if not terms:
            return ""

        return await asyncio.to_thread(
            self._search_keyword_many_sync,
            terms,
            self._scope_roots(scope),
            context_lines,
            max_results,
            max_chars,
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
                parts.append(f"[daily {current.isoformat()}]\n{text.strip()}")
            current += timedelta(days=1)

        return "\n---\n".join(parts)

    # ------------------------------------------------------------------
    # List files
    # ------------------------------------------------------------------

    async def list_files(self, scope: MemoryScope | str = "all") -> List[str]:
        """List memory entries as coordinate labels (no filesystem paths)."""
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
            return [self.root / scope_name for scope_name in _SEARCH_SCOPES]
        return [self.root / normalized_scope]

    def _label_for_path(self, path: Path) -> str:
        """Return a memory coordinate label relative to ``self.root``."""
        try:
            relative = path.resolve().relative_to(self.root.resolve())
        except ValueError:
            return "[memory]"

        parts = relative.parts
        if not parts:
            return "[memory]"

        scope = parts[0]
        stem = relative.stem

        if scope == "daily":
            return f"[daily {stem}]"
        if scope == "weekly" and "_to_" in stem:
            start, end = stem.split("_to_", 1)
            return f"[weekly {start} to {end}]"
        if scope == "monthly":
            return f"[monthly {stem}]"
        if scope == "yearly":
            return f"[yearly {stem}]"
        if scope == _NOTE_SCOPE:
            return f"[note {stem}]"
        return "[memory]"

    def _is_note_path(self, path: Path) -> bool:
        try:
            relative = path.resolve().relative_to(self.root.resolve())
        except ValueError:
            return False
        return bool(relative.parts) and relative.parts[0] == _NOTE_SCOPE

    def _event_time_for_path(self, path: Path) -> float:
        """Return epoch seconds for ranking; newer memory sorts higher on ties."""
        try:
            relative = path.resolve().relative_to(self.root.resolve())
        except ValueError:
            return 0.0

        parts = relative.parts
        if not parts:
            return 0.0

        scope = parts[0]
        stem = relative.stem
        try:
            if scope == "daily":
                event_date = date.fromisoformat(stem)
            elif scope == "weekly" and "_to_" in stem:
                _, end = stem.split("_to_", 1)
                event_date = date.fromisoformat(end)
            elif scope == "monthly" and "-" in stem:
                year_text, month_text = stem.split("-", 1)
                year = int(year_text)
                month = int(month_text)
                if month == 12:
                    event_date = date(year, 12, 31)
                else:
                    event_date = date(year, month + 1, 1) - timedelta(days=1)
            elif scope == "yearly":
                event_date = date(int(stem), 12, 31)
            elif scope == _NOTE_SCOPE:
                return path.stat().st_mtime
            else:
                return 0.0
        except ValueError:
            return 0.0
        except OSError:
            return 0.0

        return datetime.combine(event_date, datetime.min.time()).timestamp()

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

    def _list_files_sync(self, search_dir: Path) -> List[str]:
        if not search_dir.exists():
            return []
        labels = [
            self._label_for_path(path)
            for path in search_dir.rglob("*.md")
            if path.is_file()
        ]
        return sorted(set(labels))

    def _list_files_many_sync(self, search_dirs: List[Path]) -> List[str]:
        labels: list[str] = []
        for search_dir in search_dirs:
            labels.extend(self._list_files_sync(search_dir))
        return sorted(set(labels))

    def _collect_scored_blocks(
        self,
        terms: list[str],
        search_dir: Path,
        context_lines: int,
    ) -> list[tuple[int, float, str, str, int, int, list[str]]]:
        if not search_dir.exists() or not terms:
            return []

        scored_blocks: list[tuple[int, float, str, str, int, int, list[str]]] = []
        for path in sorted(search_dir.rglob("*.md")):
            if not path.is_file():
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue

            label = self._label_for_path(path)
            event_time = self._event_time_for_path(path)
            path_key = str(path)
            if self._is_note_path(path):
                window_lines = lines
                window = "\n".join(window_lines)
                score = score_text(window, terms)
                if score <= 0:
                    continue
                scored_blocks.append(
                    (score, event_time, label, path_key, 0, len(lines), window_lines)
                )
                continue
            for index, line in enumerate(lines):
                if score_text(line, terms) <= 0:
                    continue
                start = max(0, index - context_lines)
                end = min(len(lines), index + context_lines + 1)
                window_lines = lines[start:end]
                window = "\n".join(window_lines)
                scored_blocks.append(
                    (
                        score_text(window, terms),
                        event_time,
                        label,
                        path_key,
                        start,
                        end,
                        window_lines,
                    )
                )
        return scored_blocks

    @staticmethod
    def _format_scored_blocks(
        scored_blocks: list[tuple[int, float, str, str, int, int, list[str]]],
        max_results: int,
        max_chars: int = _DEFAULT_SEARCH_MAX_CHARS,
    ) -> str:
        scored_blocks.sort(key=lambda item: (-item[0], -item[1], -item[4]))
        covered_by_path: dict[str, set[int]] = {}
        blocks: list[str] = []
        used_chars = 0
        for _score, _event_time, label, path_key, start, end, plain_lines in scored_blocks:
            covered = covered_by_path.setdefault(path_key, set())
            if any(line_number in covered for line_number in range(start, end)):
                continue
            covered.update(range(start, end))
            block_text = "\n".join([label, *plain_lines])
            separator_len = len("\n---\n") if blocks else 0
            addition = separator_len + len(block_text)
            if blocks and used_chars + addition > max_chars:
                break
            if not blocks and len(block_text) > max_chars:
                blocks.append(block_text[:max_chars])
                break
            blocks.append(block_text)
            used_chars += addition
            if len(blocks) >= max_results:
                break
        return "\n---\n".join(blocks)

    def _search_keyword_sync(
        self,
        terms: list[str],
        search_dir: Path,
        context_lines: int,
        max_results: int = _DEFAULT_SEARCH_MAX_RESULTS,
        max_chars: int = _DEFAULT_SEARCH_MAX_CHARS,
    ) -> str:
        return self._format_scored_blocks(
            self._collect_scored_blocks(terms, search_dir, context_lines),
            max_results,
            max_chars,
        )

    def _search_keyword_many_sync(
        self,
        terms: list[str],
        search_dirs: List[Path],
        context_lines: int,
        max_results: int = _DEFAULT_SEARCH_MAX_RESULTS,
        max_chars: int = _DEFAULT_SEARCH_MAX_CHARS,
    ) -> str:
        scored_blocks: list[tuple[int, float, str, str, int, int, list[str]]] = []
        for search_dir in search_dirs:
            scored_blocks.extend(
                self._collect_scored_blocks(terms, search_dir, context_lines)
            )
        return self._format_scored_blocks(scored_blocks, max_results, max_chars)
