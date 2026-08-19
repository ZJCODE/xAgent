"""Atomic standing notes for the agent's own notebook.

Notes are idea cards the agent chooses to keep, not a second diary and not a
structured long-term memory schema. The diary remains the authoritative
carrier of experience; relationship cards remain the person index. A note is
rewritten in place when that idea changes. Pages connect through ``[[slug]]``
wiki links in the body; backlinks are derived at read time and never written
back into the file.

This class owns file layout, I/O, and link derivation only. Deciding *what*
belongs in a note lives in higher layers, mirroring :class:`RelationshipStore`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

NOTE_SLUG_MAX_LEN = 64
NOTE_BODY_MAX_CHARS = 2000
NOTE_MAX_PAGES = 80
NOTE_PINNED_MAX_PAGES = 2
NOTE_SUMMARY_MAX_CHARS = 80
NOTE_ARCHIVE_DIRNAME = "archive"

_META_PATTERN = re.compile(
    r'^<!--\s*note\s+(?P<attrs>.*?)\s*-->\s*$',
)
_ATTR_PATTERN = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')
_SLUG_UNSAFE = re.compile(r"[^\w.-]+", re.UNICODE)
_SLUG_HYPHEN_RUN = re.compile(r"-{2,}")
_WIKI_LINK_PATTERN = re.compile(r"\[\[([^\[\]]+)\]\]")
_TRUE_VALUES = frozenset({"true", "1", "yes"})


class NoteStoreError(ValueError):
    """Notebook mutation that the store refuses."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class NotePage:
    """A single standing note page.

    ``slug`` is the stable file key. ``body`` is the first-person markdown
    the agent wrote for its future self.
    """

    slug: str
    title: str
    body: str
    pinned: bool = False
    updated: str = ""
    accessed: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.body.strip()

    @property
    def summary(self) -> str:
        return catalog_summary(self.body)

    @property
    def touched(self) -> str:
        return self.accessed or self.updated or ""

    @property
    def links(self) -> List[str]:
        return [slug for slug in extract_wiki_links(self.body) if slug != self.slug]


def catalog_summary(body: str, max_chars: int = NOTE_SUMMARY_MAX_CHARS) -> str:
    """First meaningful line of a note, trimmed for catalog injection."""
    limit = max(1, int(max_chars or NOTE_SUMMARY_MAX_CHARS))
    for raw_line in (body or "").splitlines():
        text = raw_line.strip().lstrip("#").strip()
        if not text:
            continue
        if len(text) <= limit:
            return text
        return text[: max(1, limit - 1)] + "…"
    return ""


def slugify(value: str, *, fallback: str = "note") -> str:
    """Turn a title or slug into a filesystem-safe page id."""
    text = str(value or "").strip().lower().replace("_", "-")
    text = _SLUG_UNSAFE.sub("-", text)
    text = _SLUG_HYPHEN_RUN.sub("-", text).strip("-._")
    slug = text[:NOTE_SLUG_MAX_LEN] or fallback
    return slug or fallback


def extract_wiki_links(body: str) -> List[str]:
    """Return unique ``[[slug]]`` / ``[[slug|label]]`` targets in first-seen order."""
    ordered: List[str] = []
    seen = set()
    for match in _WIKI_LINK_PATTERN.finditer(body or ""):
        inner = match.group(1).strip()
        if not inner:
            continue
        target = inner.split("|", 1)[0].strip()
        if not target:
            continue
        slug = slugify(target)
        if not slug or slug in seen:
            continue
        seen.add(slug)
        ordered.append(slug)
    return ordered


def format_note_link_footer(slug: str, body: str, *, catalog: dict[str, str]) -> str:
    """One derived ``links:`` / ``backlinks:`` line. Never written back to disk."""
    own = slugify(slug)
    outgoing = [target for target in extract_wiki_links(body) if target != own]
    backlinks = [
        other
        for other, other_body in catalog.items()
        if other != own and own in extract_wiki_links(other_body)
    ]
    links_text = ", ".join(f"[[{item}]]" for item in outgoing) or "none"
    back_text = ", ".join(f"[[{item}]]" for item in backlinks) or "none"
    return f"links: {links_text} | backlinks: {back_text}"


class NoteStore:
    """Store standing notes as one markdown file per idea.

    Active pages live under ``<root>/<slug>.md``. Archived pages move to
    ``<root>/archive/<slug>.md``. Each file starts with a metadata comment
    owned by this store, followed by the agent-authored body. Wiki links
    stay in that body; this store derives backlinks by scanning pages.
    """

    ARCHIVE_DIRNAME = NOTE_ARCHIVE_DIRNAME

    def __init__(self, notes_dir: str) -> None:
        self.root = Path(notes_dir).expanduser()
        self._write_lock = asyncio.Lock()
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def note_path(self, slug: str) -> Path:
        return self.root / f"{self._safe_slug(slug)}.md"

    def archive_path(self, slug: str) -> Path:
        return self.root / self.ARCHIVE_DIRNAME / f"{self._safe_slug(slug)}.md"

    @classmethod
    def _safe_slug(cls, value: str) -> str:
        return slugify(value)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def read_page(self, slug: str, *, touch: bool = False) -> Optional[NotePage]:
        path = self.note_path(slug)
        text = await asyncio.to_thread(self._read_text_sync, path)
        if not text.strip():
            return None
        page = self._parse(self._safe_slug(slug), text)
        if touch and not page.is_empty:
            touched = page.__class__(
                slug=page.slug,
                title=page.title,
                body=page.body,
                pinned=page.pinned,
                updated=page.updated or date.today().isoformat(),
                accessed=date.today().isoformat(),
            )
            async with self._write_lock:
                await asyncio.to_thread(self._write_atomic_sync, path, self._render(touched))
            return touched
        return page

    async def resolve_page(
        self,
        slug_or_title: str,
        *,
        touch: bool = False,
    ) -> Optional[NotePage]:
        """Resolve a page by slug, exact title, or slugified title."""
        query = str(slug_or_title or "").strip()
        if not query:
            return None
        slug = self._safe_slug(query)
        page = await self.read_page(slug, touch=touch)
        if page is not None:
            return page
        pages = await self.list_pages()
        lowered = query.casefold()
        for candidate in pages:
            if candidate.title.casefold() == lowered:
                if touch:
                    return await self.read_page(candidate.slug, touch=True)
                return candidate
        return None

    async def list_pages(self) -> List[NotePage]:
        """List active (non-archived) notes."""
        paths = await asyncio.to_thread(self._list_active_paths_sync)
        pages: List[NotePage] = []
        for path in paths:
            text = await asyncio.to_thread(self._read_text_sync, path)
            if not text.strip():
                continue
            page = self._parse(path.stem, text)
            if not page.is_empty:
                pages.append(page)
        return pages

    async def count_pages(self) -> int:
        paths = await asyncio.to_thread(self._list_active_paths_sync)
        return len(paths)

    async def backlinks(self, slug: str) -> List[NotePage]:
        """Pages that point at ``slug`` via ``[[slug]]``. Excludes the page itself."""
        target = self._safe_slug(slug)
        pages = await self.list_pages()
        return [
            page
            for page in pages
            if page.slug != target and target in extract_wiki_links(page.body)
        ]

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def upsert_page(
        self,
        *,
        title: str,
        body: str,
        slug: Optional[str] = None,
        pinned: Optional[bool] = None,
    ) -> NotePage:
        """Create or overwrite a standing note.

        ``pinned`` is left unchanged on update when omitted. New pages default
        to unpinned. Refuses over-long bodies, a full notebook, and a third pin.
        """
        clean_title = str(title or "").strip()
        clean_body = str(body or "").strip()
        if not clean_title:
            raise NoteStoreError("empty", "Note title is required.")
        if not clean_body:
            raise NoteStoreError("empty", "Note body is required.")
        if len(clean_body) > NOTE_BODY_MAX_CHARS:
            raise NoteStoreError(
                "body_too_long",
                f"Note body exceeds {NOTE_BODY_MAX_CHARS} characters; "
                "split or compress before saving.",
            )

        resolved_slug = self._safe_slug(slug or clean_title)
        existing = await self.read_page(resolved_slug, touch=False)
        if existing is None:
            count = await self.count_pages()
            if count >= NOTE_MAX_PAGES:
                raise NoteStoreError(
                    "notebook_full",
                    f"Notebook is at the {NOTE_MAX_PAGES}-page cap; "
                    "archive or merge a page before creating another.",
                )

        want_pinned = existing.pinned if existing is not None and pinned is None else bool(pinned)
        if want_pinned:
            await self._ensure_pin_budget(resolved_slug)

        today = date.today().isoformat()
        page = NotePage(
            slug=resolved_slug,
            title=clean_title,
            body=clean_body,
            pinned=want_pinned,
            updated=today,
            accessed=today,
        )
        rendered = self._render(page)
        async with self._write_lock:
            await asyncio.to_thread(self._write_atomic_sync, self.note_path(resolved_slug), rendered)
        logger.debug("Wrote note %s (%d chars)", resolved_slug, len(clean_body))
        return page

    async def archive_page(self, slug_or_title: str) -> NotePage:
        """Move an active note into the archive directory."""
        page = await self.resolve_page(slug_or_title, touch=False)
        if page is None:
            raise NoteStoreError("not_found", f"No note matched {slug_or_title!r}.")
        source = self.note_path(page.slug)
        destination = self.archive_path(page.slug)
        archived = NotePage(
            slug=page.slug,
            title=page.title,
            body=page.body,
            pinned=False,
            updated=page.updated,
            accessed=page.accessed,
        )
        rendered = self._render(archived)
        async with self._write_lock:
            await asyncio.to_thread(self._archive_sync, source, destination, rendered)
        logger.debug("Archived note %s", page.slug)
        return archived

    async def _ensure_pin_budget(self, slug: str) -> None:
        pages = await self.list_pages()
        pinned = [page for page in pages if page.pinned and page.slug != slug]
        if len(pinned) >= NOTE_PINNED_MAX_PAGES:
            raise NoteStoreError(
                "pin_limit",
                f"At most {NOTE_PINNED_MAX_PAGES} notes can be pinned; "
                "unpin another page first.",
            )

    # ------------------------------------------------------------------
    # Render / parse
    # ------------------------------------------------------------------

    def _render(self, page: NotePage) -> str:
        updated = page.updated or date.today().isoformat()
        attrs = [
            f'slug="{self._escape(page.slug)}"',
            f'title="{self._escape(page.title)}"',
            f'updated="{self._escape(updated)}"',
        ]
        if page.accessed:
            attrs.append(f'accessed="{self._escape(page.accessed)}"')
        if page.pinned:
            attrs.append('pinned="true"')
        return f"<!-- note {' '.join(attrs)} -->\n\n{page.body.strip()}\n"

    def _parse(self, slug: str, text: str) -> NotePage:
        meta, body = self._parse_meta(text)
        resolved_slug = self._safe_slug(meta.get("slug") or slug)
        title = str(meta.get("title") or resolved_slug).strip() or resolved_slug
        pinned = str(meta.get("pinned", "")).strip().lower() in _TRUE_VALUES
        return NotePage(
            slug=resolved_slug,
            title=title,
            body=body.strip(),
            pinned=pinned,
            updated=str(meta.get("updated") or "").strip(),
            accessed=str(meta.get("accessed") or "").strip(),
        )

    @staticmethod
    def _parse_meta(text: str) -> Tuple[dict, str]:
        lines = text.splitlines()
        if not lines:
            return {}, ""
        match = _META_PATTERN.match(lines[0].strip())
        if not match:
            return {}, text
        attrs = {
            attr_match.group(1): NoteStore._unescape(attr_match.group(2))
            for attr_match in _ATTR_PATTERN.finditer(match.group("attrs"))
        }
        body = "\n".join(lines[1:]).strip()
        return attrs, body

    @staticmethod
    def _escape(value: str) -> str:
        return str(value or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")

    @staticmethod
    def _unescape(value: str) -> str:
        return value.replace('\\"', '"').replace("\\\\", "\\")

    # ------------------------------------------------------------------
    # Sync I/O primitives
    # ------------------------------------------------------------------

    def _list_active_paths_sync(self) -> List[Path]:
        if not self.root.exists():
            return []
        return sorted(
            path
            for path in self.root.glob("*.md")
            if path.is_file() and not path.name.startswith(".")
        )

    @staticmethod
    def _read_text_sync(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ""

    @staticmethod
    def _write_atomic_sync(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)

    @classmethod
    def _archive_sync(cls, source: Path, destination: Path, rendered: str) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            stamped = destination.with_name(
                f"{destination.stem}-{date.today().isoformat()}{destination.suffix}"
            )
            if stamped.exists():
                stamped = destination.with_name(
                    f"{destination.stem}-{date.today().isoformat()}-{os.getpid()}{destination.suffix}"
                )
            destination = stamped
        cls._write_atomic_sync(destination, rendered)
        try:
            source.unlink()
        except FileNotFoundError:
            pass
