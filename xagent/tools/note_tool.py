"""Tools for the agent's own notebook.

The notebook is topic-addressed memory: short, atomic, first-person conclusions
the agent wants to reuse. Writing is guarded against duplicates. Reading is
search plus opening one note and walking its links, because the point of a
Zettelkasten is that you enter at one note and follow the graph.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from xagent.components.memory.note_memory import (
    MAX_BODY_CHARS,
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
        "keys": list(note.keys),
        "updated": note.updated,
    }


def _note_detail(note: Note) -> dict:
    detail = _note_summary(note)
    detail.update({
        "body": note.body,
        "links": list(note.links),
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
            "words. One idea per note. First-person is you: name other people for their "
            "plans, preferences, and constraints; do not copy their 'I' as yours. Use it "
            "only when this turn produced a standing fact that will still hold across "
            "days — a preference, constraint, decision and what it turned on, or an "
            "approach that worked. Your diary already records what happened, and weekly "
            "distillation will surface post-hoc conclusions later — do not summarise the "
            "conversation, guess what might be useful, or do the week's processing "
            "yourself. Do not write one-off scheduling, how you stand with a person, or "
            "anything that only matters today. If it must not travel, it is not a note: "
            "put it in the diary or on a relationship card. Link related notes when you "
            "can. If a note on the topic already exists the tool says so; update that "
            "one instead."
        ),
        param_descriptions={
            "title": (
                "One line, under 80 characters, specific enough to recognise later. "
                "If the fact belongs to a person, name them."
            ),
            "body": (
                "The note in first person and your own words. 'I' is you. Attribute "
                "other people's facts to them by name. One idea, roughly 60-400 "
                "characters."
            ),
            "keys": (
                "1-5 short trigger words that would appear in a future message about this, "
                "including names. This is how the note gets recalled later, so use the "
                "surface forms people actually type."
            ),
            "links": (
                "Ids of related notes. Linking at write time is what makes the notebook "
                "navigable; prefer linking over inventing a new category. Omit if there "
                "is no clear neighbour yet."
            ),
        },
    )
    async def write_note(
        title: str,
        body: str,
        keys: Optional[list[str]] = None,
        links: Optional[list[str]] = None,
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

        similar = await store.find_similar(title=title, keys=keys, limit=3)
        duplicates = [
            note
            for note in similar
            if NoteStore.identity_score(note, title, keys)
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
                keys=tuple(keys or ()),
                links=tuple(links or ()),
                source={"diary": [today]},
                created=today,
                updated=today,
            )
        )
        return {"status": "ok", "note": _note_summary(note)}

    return write_note


def create_update_note_tool(store: NoteStore, is_enabled: bool = True):
    """Create a tool that revises an existing note."""

    @function_tool(
        name="update_note",
        description=(
            "Revise a note in your notebook. After a week of life, this is usually the "
            "right tool — not write_note: correct a conclusion that stopped holding, "
            "sharpen wording, change keys so it is recalled differently, or add or "
            "replace links to related notes. Only the fields you pass change. Prefer "
            "this over writing a second note on the same idea. When a conclusion no "
            "longer holds, rewrite this note in place; do not archive or invent a "
            "replacement id."
        ),
        param_descriptions={
            "note_id": "The 12-digit id of the note to revise.",
            "title": (
                "Replacement title, if it should change. If the fact belongs to a "
                "person, name them."
            ),
            "body": (
                "Replacement body, in first person and your own words. 'I' is you; "
                "name other people for their facts."
            ),
            "keys": "Replacement trigger words for recall.",
            "links": (
                "Replacement list of related note ids (full replace, not append). Pass "
                "the complete set you want kept."
            ),
        },
    )
    async def update_note(
        note_id: str,
        title: Optional[str] = None,
        body: Optional[str] = None,
        keys: Optional[list[str]] = None,
        links: Optional[list[str]] = None,
    ) -> dict:
        """Revise one note."""
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
                keys=tuple(keys) if keys is not None else existing.keys,
                links=tuple(links) if links is not None else existing.links,
                source=dict(existing.source),
                created=existing.created,
                updated=date.today().isoformat(),
            )
        )
        await store.write(note)
        return {"status": "ok", "note": _note_summary(note)}

    return update_note


def create_search_note_tool(store: NoteStore, is_enabled: bool = True):
    """Create a tool for searching the notebook by terms."""

    @function_tool(
        name="search_note",
        description=(
            "Search your notebook by verbatim terms. Notes already listed in your "
            "notebook index do not need searching; search when you expect a note that is "
            "not shown. Returns whole notes, since a note is already one idea. Leave "
            "query empty to browse recent notes."
        ),
        param_descriptions={
            "query": (
                "Concrete words or short phrases likely to appear in the note "
                "(e.g. [\"espresso\", \"Jun\"], not [\"drink\", \"beverage\"]). "
                "Leave empty to browse recent notes."
            ),
            "limit": f"Maximum notes to return, up to {_SEARCH_MAX_LIMIT}.",
        },
    )
    async def search_note(
        query: Optional[list[str]] = None,
        limit: int = _SEARCH_DEFAULT_LIMIT,
    ) -> dict:
        """Search the notebook. Returns matching notes."""
        if not is_enabled:
            return {"notes": [], "enabled": False, "message": "The notebook is unavailable this turn."}

        resolved_limit = max(1, min(int(limit or _SEARCH_DEFAULT_LIMIT), _SEARCH_MAX_LIMIT))
        notes = await store.search(
            terms=normalize_terms(query),
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
            "Open one note in full, including one hop of notes it links to and notes "
            "linking back. That is how you walk the notebook from a single entry point "
            "instead of searching again."
        ),
        param_descriptions={
            "note_id": "The 12-digit id of the note to open.",
        },
    )
    async def read_note(note_id: str) -> dict:
        """Open one note with its immediate neighbours."""
        if not is_enabled:
            return {"status": "disabled", "message": "The notebook is unavailable this turn."}

        note = await store.read(str(note_id or "").strip())
        if note is None:
            return {"status": "not_found", "message": f"No note with id {note_id}."}

        neighbours = await store.neighbours(note, limit=_NEIGHBOUR_LIMIT)
        return {
            "status": "ok",
            "note": _note_detail(note),
            "neighbours": [
                {**_note_summary(neighbour), "snippet": neighbour.snippet}
                for neighbour in neighbours
            ],
        }

    return read_note
