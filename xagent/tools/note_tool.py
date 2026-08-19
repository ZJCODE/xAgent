"""Tools for the agent's own notebook.

The notebook is topic-addressed memory: short, atomic, first-person conclusions
the agent wants to reuse. Writing is guarded against duplicates, reading comes
in two flavours (term search and link following), because the point of a
Zettelkasten is that you enter at one note and walk the links.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from xagent.components.memory.note_memory import (
    MAX_BODY_CHARS,
    STATUS_ARCHIVED,
    Note,
    NoteStore,
)
from xagent.core.config import AgentConfig
from xagent.utils.search_terms import normalize_terms
from xagent.utils.tool_decorator import function_tool

_SEARCH_DEFAULT_LIMIT = 8
_SEARCH_MAX_LIMIT = 20
_NEIGHBOUR_LIMIT = 3


def _note_summary(note: Note) -> dict:
    """Compact note view returned by search and write results."""
    return {
        "id": note.id,
        "title": note.title,
        "tags": list(note.tags),
        "sensitivity": note.sensitivity,
        "updated": note.updated,
    }


def _note_detail(note: Note) -> dict:
    detail = _note_summary(note)
    detail.update({
        "body": note.body,
        "kind": note.kind,
        "status": note.status,
        "keys": list(note.keys),
        "links": list(note.links),
        "pinned": note.pinned,
        "created": note.created,
    })
    if note.source:
        detail["source"] = dict(note.source)
    return detail


def create_write_note_tool(store: NoteStore, is_enabled: bool = True):
    """Create a tool that adds one note to the notebook."""

    @function_tool(
        name="write_note",
        description=(
            "Add one note to your notebook: a durable, reusable conclusion in your own "
            "words. One idea per note. Use it for preferences, constraints, decisions and "
            "what they turned on, or an approach that worked. Your diary already records "
            "what happened, so do not use this to summarise a conversation. If a note on "
            "the topic already exists the tool says so; update that one instead."
        ),
        param_descriptions={
            "title": "One line, under 80 characters, specific enough to recognise later.",
            "body": (
                "The note in first person and your own words. One idea, roughly 60-400 "
                "characters."
            ),
            "keys": (
                "1-5 short trigger words that would appear in a future message about this, "
                "including names. This is how the note gets recalled later, so use the "
                "surface forms people actually type."
            ),
            "tags": "0-3 short reusable topic labels.",
            "links": "Ids of related notes. Linking is what makes the notebook navigable.",
            "sensitivity": (
                "shareable (general knowledge), person-scoped (belongs to one person's "
                "context, must not travel), or private (yours alone)."
            ),
            "kind": (
                "note for an idea, hub for an entry point that links a cluster together, "
                "ref for a digest of an external source."
            ),
        },
    )
    async def write_note(
        title: str,
        body: str,
        keys: Optional[list[str]] = None,
        tags: Optional[list[str]] = None,
        links: Optional[list[str]] = None,
        sensitivity: str = "shareable",
        kind: str = "note",
    ) -> dict:
        """Add one note to the notebook."""
        if not is_enabled:
            return {"status": "disabled", "message": "The notebook is unavailable this turn."}

        title = str(title or "").strip()
        body = str(body or "").strip()
        if not title or not body:
            return {"status": "skipped", "message": "A note needs both a title and a body."}
        if len(body) > MAX_BODY_CHARS:
            return {
                "status": "too_long",
                "message": (
                    f"A note body must stay under {MAX_BODY_CHARS} characters. "
                    "Split this into separate notes, one idea each."
                ),
            }

        similar = await store.find_similar(title=title, keys=keys, tags=tags, limit=3)
        duplicates = [
            note
            for note in similar
            if NoteStore.identity_score(note, title, keys, tags)
            >= AgentConfig.NOTES_DUPLICATE_SCORE_THRESHOLD
        ]
        if duplicates:
            return {
                "status": "similar_exists",
                "message": (
                    "The notebook already has a note on this. Use update_note to revise it, "
                    "or write_note again with a clearly different title if this really is a "
                    "separate idea."
                ),
                "candidates": [_note_summary(note) for note in duplicates],
            }

        today = date.today().isoformat()
        note = await store.create(
            Note(
                id="",
                title=title,
                body=body,
                kind=str(kind or "note"),
                tags=tuple(tags or ()),
                keys=tuple(keys or ()),
                links=tuple(links or ()),
                sensitivity=str(sensitivity or "shareable"),
                source={"diary": [today]},
                created=today,
                updated=today,
            )
        )
        return {"status": "ok", "note": _note_summary(note)}

    return write_note


def create_update_note_tool(store: NoteStore, is_enabled: bool = True):
    """Create a tool that revises or archives an existing note."""

    @function_tool(
        name="update_note",
        description=(
            "Revise a note in your notebook: correct it, sharpen it, add links, pin it, or "
            "archive it when it stopped being true. Only the fields you pass change. "
            "Prefer this over writing a second note on the same idea."
        ),
        param_descriptions={
            "note_id": "The 12-digit id of the note to revise.",
            "title": "Replacement title, if it should change.",
            "body": "Replacement body, in first person and your own words.",
            "keys": "Replacement trigger words for recall.",
            "tags": "Replacement topic labels.",
            "links": "Replacement list of related note ids.",
            "sensitivity": "shareable, person-scoped, or private.",
            "pinned": (
                "Pin a note to keep it in mind every turn. Reserve this for the few notes "
                "that should always be present."
            ),
            "archive": (
                "Archive the note when it no longer holds. Archived notes are kept but "
                "stop being recalled."
            ),
        },
    )
    async def update_note(
        note_id: str,
        title: Optional[str] = None,
        body: Optional[str] = None,
        keys: Optional[list[str]] = None,
        tags: Optional[list[str]] = None,
        links: Optional[list[str]] = None,
        sensitivity: Optional[str] = None,
        pinned: Optional[bool] = None,
        archive: bool = False,
    ) -> dict:
        """Revise or archive one note."""
        if not is_enabled:
            return {"status": "disabled", "message": "The notebook is unavailable this turn."}

        existing = await store.read(str(note_id or "").strip())
        if existing is None:
            return {"status": "not_found", "message": f"No note with id {note_id}."}

        if body is not None and len(str(body).strip()) > MAX_BODY_CHARS:
            return {
                "status": "too_long",
                "message": (
                    f"A note body must stay under {MAX_BODY_CHARS} characters. "
                    "Split this into separate notes, one idea each."
                ),
            }

        note = NoteStore.normalize(
            Note(
                id=existing.id,
                title=str(title).strip() if title is not None else existing.title,
                body=str(body).strip() if body is not None else existing.body,
                kind=existing.kind,
                status=STATUS_ARCHIVED if archive else existing.status,
                tags=tuple(tags) if tags is not None else existing.tags,
                keys=tuple(keys) if keys is not None else existing.keys,
                links=tuple(links) if links is not None else existing.links,
                pinned=bool(pinned) if pinned is not None else existing.pinned,
                sensitivity=(
                    str(sensitivity) if sensitivity is not None else existing.sensitivity
                ),
                source=dict(existing.source),
                created=existing.created,
                updated=date.today().isoformat(),
            )
        )
        await store.write(note)
        return {"status": "ok", "note": _note_summary(note)}

    return update_note


def create_search_note_tool(store: NoteStore, is_enabled: bool = True):
    """Create a tool for searching the notebook by terms or tags."""

    @function_tool(
        name="search_note",
        description=(
            "Search your notebook by verbatim terms or tags. Notes already listed in your "
            "notebook index do not need searching; search when you expect a note that is "
            "not shown. Returns whole notes, since a note is already one idea."
        ),
        param_descriptions={
            "query": (
                "Concrete words or short phrases likely to appear in the note "
                "(e.g. [\"espresso\", \"Jun\"], not [\"drink\", \"beverage\"]). "
                "Leave empty to browse by tag or kind."
            ),
            "tags": "Restrict to notes carrying any of these tags.",
            "kind": "Restrict to note, hub, or ref.",
            "limit": f"Maximum notes to return, up to {_SEARCH_MAX_LIMIT}.",
        },
    )
    async def search_note(
        query: Optional[list[str]] = None,
        tags: Optional[list[str]] = None,
        kind: str = "",
        limit: int = _SEARCH_DEFAULT_LIMIT,
    ) -> dict:
        """Search the notebook. Returns matching notes."""
        if not is_enabled:
            return {"notes": [], "enabled": False, "message": "The notebook is unavailable this turn."}

        resolved_limit = max(1, min(int(limit or _SEARCH_DEFAULT_LIMIT), _SEARCH_MAX_LIMIT))
        notes = await store.search(
            terms=normalize_terms(query),
            tags=tags,
            kind=kind,
            limit=resolved_limit,
        )
        return {
            "notes": [_note_detail(note) for note in notes],
            "enabled": True,
            "total": await store.count(),
        }

    return search_note


def create_read_note_tool(store: NoteStore, is_enabled: bool = True):
    """Create a tool for opening one note and walking its links."""

    @function_tool(
        name="read_note",
        description=(
            "Open one note in full. With follow_links it also returns the notes it links "
            "to and the notes linking back, which is how you walk the notebook from a "
            "single entry point instead of searching again."
        ),
        param_descriptions={
            "note_id": "The 12-digit id of the note to open.",
            "follow_links": "Also return one hop of linked and linking notes.",
        },
    )
    async def read_note(note_id: str, follow_links: bool = False) -> dict:
        """Open one note, optionally with its immediate neighbours."""
        if not is_enabled:
            return {"status": "disabled", "message": "The notebook is unavailable this turn."}

        note = await store.read(str(note_id or "").strip())
        if note is None:
            return {"status": "not_found", "message": f"No note with id {note_id}."}

        result = {"status": "ok", "note": _note_detail(note)}
        if follow_links:
            neighbours = await store.neighbours(note, limit=_NEIGHBOUR_LIMIT)
            result["neighbours"] = [
                {**_note_summary(neighbour), "snippet": neighbour.snippet}
                for neighbour in neighbours
            ]
        return result

    return read_note
