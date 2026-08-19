"""Topic-addressed notebook derived from the unified diary memory.

Notes are the agent's own notebook: short, atomic, first-person conclusions it
wants to reuse instead of re-deriving from a year of diary every turn. Like
:class:`RelationshipStore`, the notebook is a *derived view* over the single
diary stream rather than a second source of truth — every note carries the
provenance it came from, and the system keeps working when the notebook is
empty or thrown away.

Zettelkasten supplies the shape: one idea per note, an immutable id so links
never break, links instead of a category tree, and hub notes as entry points
into a cluster.

This class owns file layout and I/O only. Deciding *what* is worth a note and
*when* to write one lives in higher layers (note tools, journal service, memory
handler), mirroring how :class:`MarkdownMemory` separates storage from policy.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

from xagent.utils.search_terms import normalize_terms, score_text

logger = logging.getLogger(__name__)

KIND_NOTE = "note"
KIND_HUB = "hub"
KIND_REF = "ref"
VALID_KINDS = (KIND_NOTE, KIND_HUB, KIND_REF)

STATUS_ACTIVE = "active"
STATUS_ARCHIVED = "archived"
VALID_STATUSES = (STATUS_ACTIVE, STATUS_ARCHIVED)

SENSITIVITY_SHAREABLE = "shareable"
SENSITIVITY_PERSON_SCOPED = "person-scoped"
SENSITIVITY_PRIVATE = "private"
VALID_SENSITIVITIES = (
    SENSITIVITY_SHAREABLE,
    SENSITIVITY_PERSON_SCOPED,
    SENSITIVITY_PRIVATE,
)

MAX_TITLE_CHARS = 80
MAX_BODY_CHARS = 2000
MAX_TAGS = 5
MAX_KEYS = 5
MIN_KEY_CHARS = 2

_ID_PATTERN = re.compile(r"^(\d{12})")
_SLUG_UNSAFE = re.compile(r"[^a-zA-Z0-9._-]+")
_WIKI_LINK_PATTERN = re.compile(r"\[\[\s*(\d{12})\s*\]\]")
_SOURCE_FIELDS = ("diary", "person", "cursor", "url", "tool")


@dataclass(frozen=True)
class Note:
    """One atomic notebook entry.

    ``id`` is a 12-digit ``YYYYMMDDHHMM`` stamp that never changes, so titles
    can be rewritten without breaking links. ``body`` is first-person prose in
    the agent's own words.
    """

    id: str
    title: str
    body: str
    kind: str = KIND_NOTE
    status: str = STATUS_ACTIVE
    tags: Tuple[str, ...] = ()
    keys: Tuple[str, ...] = ()
    links: Tuple[str, ...] = ()
    pinned: bool = False
    sensitivity: str = SENSITIVITY_SHAREABLE
    source: Dict[str, Any] = field(default_factory=dict)
    created: str = ""
    updated: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.body.strip() and not self.title.strip()

    @property
    def is_archived(self) -> bool:
        return self.status == STATUS_ARCHIVED

    @property
    def match_keys(self) -> Tuple[str, ...]:
        """Trigger surfaces used to recall this note from an incoming message.

        The note declares how it wants to be found, which keeps auto-recall
        free of a tokenizer: instead of splitting the message into terms, the
        message is scanned for these keys.
        """
        seen: set[str] = set()
        ordered: List[str] = []
        for candidate in (*self.keys, *self.tags):
            normalized = str(candidate or "").strip()
            if len(normalized) < MIN_KEY_CHARS:
                continue
            folded = normalized.casefold()
            if folded in seen:
                continue
            seen.add(folded)
            ordered.append(normalized)
        return tuple(ordered)

    @property
    def snippet(self) -> str:
        """First paragraph line, for index rows that show a taste of the body."""
        for line in self.body.splitlines():
            text = line.strip()
            if text:
                return text
        return ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "body": self.body,
            "kind": self.kind,
            "status": self.status,
            "tags": list(self.tags),
            "keys": list(self.keys),
            "links": list(self.links),
            "pinned": self.pinned,
            "sensitivity": self.sensitivity,
            "source": dict(self.source),
            "created": self.created,
            "updated": self.updated,
        }


class NoteStore:
    """Store notes as one markdown file per note under a flat directory.

    Files are ``<root>/<id>-<slug>.md`` with YAML frontmatter followed by the
    note body, matching the ``SKILL.md`` convention. The id in the frontmatter
    is authoritative; the filename slug is a browsing convenience and may be
    absent for titles that produce no ASCII slug.

    Nothing that changes on read is written back into a note file, so note
    files stay stable, diffable, and safe to hand-edit.
    """

    def __init__(self, notes_dir: str) -> None:
        self.root = Path(notes_dir).expanduser()
        self._write_lock = asyncio.Lock()
        self.root.mkdir(parents=True, exist_ok=True)
        self._cache_signature: Optional[Tuple[Any, ...]] = None
        self._cache_notes: Tuple[Note, ...] = ()

    # ------------------------------------------------------------------
    # Id / path helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_segment(value: str) -> str:
        sanitized = _SLUG_UNSAFE.sub("-", str(value or "").strip()).strip("-.")
        return sanitized[:48].lower()

    def _file_name(self, note_id: str, title: str) -> str:
        slug = self._safe_segment(title)
        return f"{note_id}-{slug}.md" if slug else f"{note_id}.md"

    def path_for(self, note_id: str, title: str = "") -> Path:
        """Return the existing file for *note_id*, else the path to create.

        The id owns identity, so an existing file keeps its name even when the
        title is rewritten. Renaming would churn paths for no gain and risks
        leaving two files for one id.
        """
        for path in sorted(self.root.glob(f"{note_id}*.md")):
            if path.is_file():
                return path
        return self.root / self._file_name(note_id, title)

    def next_id(self, now: Optional[datetime] = None) -> str:
        """Return an unused 12-digit id, walking forward on collision."""
        stamp = (now or datetime.now()).replace(second=0, microsecond=0)
        existing = self._existing_ids_sync()
        for _ in range(1440):
            candidate = stamp.strftime("%Y%m%d%H%M")
            if candidate not in existing:
                return candidate
            stamp += timedelta(minutes=1)
        return stamp.strftime("%Y%m%d%H%M")

    def _existing_ids_sync(self) -> set[str]:
        ids: set[str] = set()
        if not self.root.exists():
            return ids
        for path in self.root.glob("*.md"):
            match = _ID_PATTERN.match(path.name)
            if match:
                ids.add(match.group(1))
        return ids

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def read(self, note_id: str) -> Optional[Note]:
        """Read one note by id."""
        normalized = str(note_id or "").strip()
        if not normalized:
            return None
        notes = await self.list_notes(include_archived=True)
        for note in notes:
            if note.id == normalized:
                return note
        return None

    async def list_notes(self, include_archived: bool = False) -> List[Note]:
        """Return all notes, newest id first."""
        notes = await asyncio.to_thread(self._load_all_sync)
        if include_archived:
            return list(notes)
        return [note for note in notes if not note.is_archived]

    async def backlinks(self, note_id: str) -> List[Note]:
        """Return notes linking to *note_id*."""
        normalized = str(note_id or "").strip()
        if not normalized:
            return []
        notes = await self.list_notes()
        return [note for note in notes if normalized in note.links]

    async def neighbours(self, note: Note, limit: int = 3) -> List[Note]:
        """Return one hop of links plus backlinks, outbound links first."""
        notes = {item.id: item for item in await self.list_notes()}
        ordered: List[Note] = []
        seen = {note.id}
        for link_id in note.links:
            linked = notes.get(link_id)
            if linked is not None and linked.id not in seen:
                seen.add(linked.id)
                ordered.append(linked)
        for candidate in notes.values():
            if candidate.id in seen:
                continue
            if note.id in candidate.links:
                seen.add(candidate.id)
                ordered.append(candidate)
        return ordered[: max(0, int(limit))]

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    async def recall(self, text: str, limit: int = 4) -> List[Note]:
        """Recall notes whose declared keys appear in *text*.

        The match runs in reverse — each note's own trigger keys are searched
        for inside the message — so no tokenizer is needed and Chinese behaves
        the same as English.
        """
        haystack = str(text or "").strip()
        if not haystack:
            return []

        scored: List[Tuple[int, int, str, str, Note]] = []
        for note in await self.list_notes():
            keys = note.match_keys
            if not keys:
                continue
            hits = score_text(haystack, list(keys))
            if hits <= 0:
                continue
            scored.append(
                (hits, 1 if note.pinned else 0, note.updated or note.id, note.id, note)
            )
        scored.sort(key=lambda item: item[:4], reverse=True)
        return [item[4] for item in scored[: max(0, int(limit))]]

    async def search(
        self,
        terms: Sequence[str],
        tags: Optional[Sequence[str]] = None,
        kind: str = "",
        include_archived: bool = False,
        limit: int = 10,
    ) -> List[Note]:
        """Forward search over title, keys, tags, and body."""
        normalized_terms = normalize_terms(list(terms or []))
        wanted_tags = {
            str(tag).strip().casefold()
            for tag in (tags or [])
            if str(tag).strip()
        }
        wanted_kind = str(kind or "").strip().lower()

        scored: List[Tuple[int, str, str, Note]] = []
        for note in await self.list_notes(include_archived=include_archived):
            if wanted_kind and note.kind != wanted_kind:
                continue
            if wanted_tags and not wanted_tags & {tag.casefold() for tag in note.tags}:
                continue
            if not normalized_terms:
                score = 1
            else:
                score = (
                    3 * score_text(note.title, normalized_terms)
                    + 2 * score_text(" ".join((*note.keys, *note.tags)), normalized_terms)
                    + score_text(note.body, normalized_terms)
                )
                if score <= 0:
                    continue
            if note.pinned:
                score += 1
            scored.append((score, note.updated or note.id, note.id, note))
        scored.sort(key=lambda item: item[:3], reverse=True)
        return [item[3] for item in scored[: max(0, int(limit))]]

    async def find_similar(
        self,
        title: str,
        keys: Optional[Sequence[str]] = None,
        tags: Optional[Sequence[str]] = None,
        limit: int = 3,
    ) -> List[Note]:
        """Find notes close enough that a new note would probably duplicate one.

        Used as a pre-write guard so the notebook does not accumulate five
        variations of the same idea.
        """
        terms = normalize_terms([
            str(title or ""),
            *[str(key) for key in (keys or [])],
            *[str(tag) for tag in (tags or [])],
        ])
        if not terms:
            return []
        candidates = await self.search(terms=terms, limit=max(1, int(limit)))
        return candidates

    async def pinned(self, limit: int = 3) -> List[Note]:
        """Return pinned notes, most recently updated first."""
        notes = [note for note in await self.list_notes() if note.pinned]
        notes.sort(key=lambda note: (note.updated or note.id, note.id), reverse=True)
        return notes[: max(0, int(limit))]

    async def hubs(self, limit: int = 5) -> List[Note]:
        """Return hub notes, most recently updated first."""
        notes = [note for note in await self.list_notes() if note.kind == KIND_HUB]
        notes.sort(key=lambda note: (note.updated or note.id, note.id), reverse=True)
        return notes[: max(0, int(limit))]

    async def count(self, include_archived: bool = False) -> int:
        return len(await self.list_notes(include_archived=include_archived))

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def create(self, note: Note) -> Note:
        """Assign an id and write the note, holding one lock across both.

        Allocating the id outside the lock would let two notes written in the
        same minute claim the same id, and the second write would silently
        replace the first.
        """
        async with self._write_lock:
            prepared = self.normalize(replace(note, id=self.next_id()))
            path = self.path_for(prepared.id, prepared.title)
            await asyncio.to_thread(self._write_atomic_sync, path, self._render(prepared))
            self._cache_signature = None
        logger.debug("Created note %s (%d chars)", prepared.id, len(prepared.body))
        return prepared

    async def write(self, note: Note) -> Path:
        """Write (overwrite) one note atomically. The note must already have an id."""
        path = self.path_for(note.id, note.title)
        rendered = self._render(note)
        async with self._write_lock:
            await asyncio.to_thread(self._write_atomic_sync, path, rendered)
            self._cache_signature = None
        logger.debug("Wrote note %s (%d chars)", note.id, len(note.body))
        return path

    async def archive(self, note_id: str) -> Optional[Note]:
        """Mark a note archived. Notes are never deleted, so the trail survives."""
        note = await self.read(note_id)
        if note is None:
            return None
        archived = self.normalize(
            replace(note, status=STATUS_ARCHIVED, updated=date.today().isoformat())
        )
        await self.write(archived)
        return archived

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------

    @classmethod
    def normalize(cls, note: Note) -> Note:
        """Clamp a note to the schema so bad input cannot corrupt the store."""
        today = date.today().isoformat()
        kind = str(note.kind or KIND_NOTE).strip().lower()
        status = str(note.status or STATUS_ACTIVE).strip().lower()
        sensitivity = str(note.sensitivity or SENSITIVITY_SHAREABLE).strip().lower()
        return Note(
            id=str(note.id or "").strip(),
            title=cls._clean_line(note.title)[:MAX_TITLE_CHARS],
            body=str(note.body or "").strip()[:MAX_BODY_CHARS],
            kind=kind if kind in VALID_KINDS else KIND_NOTE,
            status=status if status in VALID_STATUSES else STATUS_ACTIVE,
            tags=cls._clean_list(note.tags, MAX_TAGS),
            keys=cls._clean_list(note.keys, MAX_KEYS, min_chars=MIN_KEY_CHARS),
            links=cls._clean_ids(note.links),
            pinned=bool(note.pinned),
            sensitivity=(
                sensitivity if sensitivity in VALID_SENSITIVITIES else SENSITIVITY_SHAREABLE
            ),
            source=cls._clean_source(note.source),
            created=cls._clean_line(note.created) or today,
            updated=cls._clean_line(note.updated) or today,
        )

    @staticmethod
    def _clean_line(value: Any) -> str:
        return " ".join(str(value or "").split())

    @classmethod
    def _clean_list(cls, values: Any, max_items: int, min_chars: int = 1) -> Tuple[str, ...]:
        if isinstance(values, str):
            values = [values]
        cleaned: List[str] = []
        seen: set[str] = set()
        for value in values or []:
            normalized = cls._clean_line(value)
            if len(normalized) < min_chars:
                continue
            folded = normalized.casefold()
            if folded in seen:
                continue
            seen.add(folded)
            cleaned.append(normalized)
            if len(cleaned) >= max_items:
                break
        return tuple(cleaned)

    @staticmethod
    def _clean_ids(values: Any) -> Tuple[str, ...]:
        if isinstance(values, (str, int)):
            values = [values]
        cleaned: List[str] = []
        for value in values or []:
            text = str(value or "").strip()
            match = _ID_PATTERN.match(text)
            if match and match.group(1) not in cleaned:
                cleaned.append(match.group(1))
        return tuple(cleaned)

    @classmethod
    def _clean_source(cls, source: Any) -> Dict[str, Any]:
        if not isinstance(source, Mapping):
            return {}
        cleaned: Dict[str, Any] = {}
        for key in _SOURCE_FIELDS:
            if key not in source:
                continue
            value = source[key]
            if key == "diary":
                dates = [cls._clean_line(item) for item in (value or [])] if not isinstance(value, str) else [cls._clean_line(value)]
                dates = [item for item in dates if item]
                if dates:
                    cleaned["diary"] = dates
                continue
            if key == "cursor":
                try:
                    cursor = int(value)
                except (TypeError, ValueError):
                    continue
                if cursor > 0:
                    cleaned["cursor"] = cursor
                continue
            text = cls._clean_line(value)
            if text:
                cleaned[key] = text
        return cleaned

    # ------------------------------------------------------------------
    # Render / parse
    # ------------------------------------------------------------------

    @classmethod
    def _render(cls, note: Note) -> str:
        normalized = cls.normalize(note)
        frontmatter: Dict[str, Any] = {
            "id": normalized.id,
            "title": normalized.title,
            "kind": normalized.kind,
            "status": normalized.status,
        }
        if normalized.tags:
            frontmatter["tags"] = list(normalized.tags)
        if normalized.keys:
            frontmatter["keys"] = list(normalized.keys)
        if normalized.links:
            frontmatter["links"] = list(normalized.links)
        if normalized.pinned:
            frontmatter["pinned"] = True
        frontmatter["sensitivity"] = normalized.sensitivity
        if normalized.source:
            frontmatter["source"] = normalized.source
        frontmatter["created"] = normalized.created
        frontmatter["updated"] = normalized.updated

        header = yaml.safe_dump(
            frontmatter,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        ).strip()
        return f"---\n{header}\n---\n\n{normalized.body}\n"

    @classmethod
    def _parse(cls, path: Path, text: str) -> Optional[Note]:
        """Parse a note file, tolerating hand-edited damage.

        A file with broken or missing frontmatter still yields a usable note
        built from the filename id and the raw text, because humans edit these
        files directly and a syntax slip must not swallow a note.
        """
        fallback_id = ""
        match = _ID_PATTERN.match(path.name)
        if match:
            fallback_id = match.group(1)

        frontmatter, body = cls._split_frontmatter(text)
        if not isinstance(frontmatter, dict):
            frontmatter = {}

        note_id = str(frontmatter.get("id") or fallback_id or "").strip()
        if not note_id:
            logger.warning("Skipping note without a usable id: %s", path.name)
            return None

        links = list(frontmatter.get("links") or [])
        links.extend(_WIKI_LINK_PATTERN.findall(body))

        title = frontmatter.get("title") or cls._title_from_body(body) or note_id
        note = Note(
            id=note_id,
            title=str(title),
            body=body,
            kind=str(frontmatter.get("kind") or KIND_NOTE),
            status=str(frontmatter.get("status") or STATUS_ACTIVE),
            tags=tuple(str(tag) for tag in (frontmatter.get("tags") or [])),
            keys=tuple(str(key) for key in (frontmatter.get("keys") or [])),
            links=tuple(str(link) for link in links),
            pinned=bool(frontmatter.get("pinned")),
            sensitivity=str(frontmatter.get("sensitivity") or SENSITIVITY_SHAREABLE),
            source=frontmatter.get("source") if isinstance(frontmatter.get("source"), Mapping) else {},
            created=str(frontmatter.get("created") or ""),
            updated=str(frontmatter.get("updated") or ""),
        )
        normalized = cls.normalize(note)
        if normalized.is_empty:
            return None
        return normalized

    @staticmethod
    def _split_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
        lines = str(text or "").splitlines(keepends=True)
        if not lines or lines[0].strip() != "---":
            return {}, str(text or "").strip()
        closing_index = None
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                closing_index = index
                break
        if closing_index is None:
            return {}, str(text or "").strip()
        raw = "".join(lines[1:closing_index])
        body = "".join(lines[closing_index + 1:]).strip()
        try:
            loaded = yaml.safe_load(raw) or {}
        except yaml.YAMLError as exc:
            logger.warning("Note frontmatter is not valid YAML, falling back to body only: %s", exc)
            return {}, body
        if not isinstance(loaded, dict):
            return {}, body
        return loaded, body

    @staticmethod
    def _title_from_body(body: str) -> str:
        for line in str(body or "").splitlines():
            text = line.strip().lstrip("#").strip()
            if text:
                return text[:MAX_TITLE_CHARS]
        return ""

    # ------------------------------------------------------------------
    # Sync I/O primitives
    # ------------------------------------------------------------------

    def _load_all_sync(self) -> Tuple[Note, ...]:
        signature = self._signature_sync()
        if signature == self._cache_signature:
            return self._cache_notes

        notes: List[Note] = []
        for path in sorted(self.root.glob("*.md")):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.warning("Failed to read note %s: %s", path.name, exc)
                continue
            note = self._parse(path, text)
            if note is not None:
                notes.append(note)
        notes.sort(key=lambda note: note.id, reverse=True)

        loaded = tuple(notes)
        self._cache_signature = signature
        self._cache_notes = loaded
        return loaded

    def _signature_sync(self) -> Tuple[Any, ...]:
        """Cheap directory fingerprint: names plus mtimes, no file reads."""
        if not self.root.exists():
            return ()
        entries: List[Tuple[str, int, int]] = []
        try:
            with os.scandir(self.root) as scanner:
                for entry in scanner:
                    if not entry.name.endswith(".md") or not entry.is_file():
                        continue
                    stat = entry.stat()
                    entries.append((entry.name, stat.st_mtime_ns, stat.st_size))
        except OSError as exc:
            logger.warning("Failed to scan notes directory: %s", exc)
            return ()
        return tuple(sorted(entries))

    @staticmethod
    def _write_atomic_sync(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
