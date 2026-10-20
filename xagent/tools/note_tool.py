"""Find notebook cards, then open a card and follow its references."""

from typing import Optional

from xagent.components.memory.note_memory import Note, NoteStore
from xagent.core.config import AgentConfig
from xagent.utils.search_terms import normalize_terms
from xagent.utils.tool_decorator import function_tool

_SEARCH_DEFAULT_LIMIT = 8
_SEARCH_MAX_LIMIT = 20
_NEIGHBOUR_LIMIT = 3


def note_summary(note: Note) -> dict:
    text = " ".join(note.body.split())
    limit = AgentConfig.NOTEBOOK_SNIPPET_MAX_CHARS
    return {
        "id": note.id,
        "title": note.title,
        "snippet": text[:limit].rstrip() + ("..." if len(text) > limit else ""),
    }


async def related_summaries(store: NoteStore, note: Note) -> list[dict]:
    summaries = []
    for neighbour in await store.neighbours(note, limit=_NEIGHBOUR_LIMIT):
        outgoing = neighbour.id in note.links
        incoming = note.id in neighbour.links
        summaries.append({
            **note_summary(neighbour),
            "direction": "both" if outgoing and incoming else "outgoing" if outgoing else "incoming",
        })
    return summaries


def create_search_note_tool(store: NoteStore):
    @function_tool(
        name="search_note",
        description=(
            "Find notebook cards by concrete words or short phrases. Returns ids, "
            "titles and short summaries; open a card with read_note for its full "
            "conclusion, sources and references. Skip searching when the notebook "
            "index already gives you the id. An empty query browses recent cards."
        ),
        param_descriptions={
            "query": "Verbatim words or phrases, e.g. [\"SQLite\", \"offline\"]. Empty browses recent cards.",
            "limit": f"Maximum results, up to {_SEARCH_MAX_LIMIT}.",
        },
    )
    async def search_note(query: Optional[list[str]] = None, limit: int = _SEARCH_DEFAULT_LIMIT) -> dict:
        resolved_limit = max(1, min(int(limit or _SEARCH_DEFAULT_LIMIT), _SEARCH_MAX_LIMIT))
        notes = await store.search(terms=normalize_terms(query), limit=resolved_limit)
        return {"notes": [note_summary(note) for note in notes], "total": await store.count()}

    return search_note


def create_read_note_tool(store: NoteStore):
    @function_tool(
        name="read_note",
        description=(
            "Open one notebook card by id: its full conclusion, diary sources, and "
            "up to three summaries of cards it references or that reference it. "
            "Directions are outgoing, incoming, or both. Open another id to follow "
            "a reference; references are never expanded recursively."
        ),
        param_descriptions={"note_id": "The 12-digit id of the card to open."},
    )
    async def read_note(note_id: str) -> dict:
        note = await store.read(str(note_id or "").strip())
        if note is None:
            return {"status": "not_found", "message": f"No note with id {note_id}."}
        return {"status": "ok", "note": note.to_dict(), "neighbours": await related_summaries(store, note)}

    return read_note
