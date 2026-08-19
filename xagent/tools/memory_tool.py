"""Dedicated tools for long-term memory access."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from xagent.components.memory.note_memory import NoteStoreError
from xagent.utils.search_terms import normalize_terms, score_text
from xagent.utils.tool_decorator import function_tool

if TYPE_CHECKING:
    from xagent.components.memory import MarkdownMemory, NoteStore
    from xagent.components.message import MessageStorage

_SEARCH_MAX_RESULTS = 20
_SEARCH_MAX_RESULTS_WITH_DATE = 30
_SEARCH_MAX_CHARS = 6000


def create_write_memory_tool(
    memory: MarkdownMemory,
    is_enabled: bool = True,
):
    """Create a tool that records long-term useful memory."""

    @function_tool(
        name="write_memory",
        description=(
            "Record a concise, attributable diary entry for what happened: events, "
            "conversations, decisions, or context in time. Skip standing topic knowledge "
            "— use upsert_note for current-version notes you will reuse. Skip trivial "
            "or temporary notes."
        ),
        param_descriptions={
            "content": "Diary entry to record. Keep it concise, grounded, and attributed when needed.",
        },
    )
    async def write_memory(content: str) -> dict:
        """Record a long-term diary entry."""
        if not is_enabled:
            return {"status": "disabled", "message": "Memory writing is disabled for this turn."}

        content = content.strip()
        if not content:
            return {"status": "skipped", "message": "Empty content, nothing written."}

        await memory.append_daily(content)
        return {"status": "ok", "message": "Memory recorded."}

    return write_memory


def create_upsert_note_tool(
    note_store: NoteStore,
    is_enabled: bool = True,
):
    """Create a tool that writes standing topic notes."""

    @function_tool(
        name="upsert_note",
        description=(
            "Create or rewrite a standing notebook page for current-version topic knowledge "
            "you will reuse (procedures, checklists, cross-person conventions, your own "
            "working habits). Overwrites the page in place. Do not use this for what happened "
            "today (write_memory) or for who one person is (relationship cards). Skip anything "
            "you are unsure you will reuse. Pin at most two pages that must stay fully in context."
        ),
        param_descriptions={
            "title": "Short page title. Used to generate the slug when slug is omitted.",
            "content": "Full current-version page body in first person. Replace the whole page.",
            "slug": "Stable page id. Omit to derive from the title. Reuse a slug to rewrite that page.",
            "pin": "If true, keep this page body in context (max two pinned pages). If false, unpin.",
        },
    )
    async def upsert_note(
        title: str,
        content: str,
        slug: Optional[str] = None,
        pin: Optional[bool] = None,
    ) -> dict:
        """Create or overwrite a standing note."""
        if not is_enabled:
            return {"status": "disabled", "message": "Notebook writing is disabled for this turn."}
        try:
            page = await note_store.upsert_page(
                title=title,
                body=content,
                slug=slug,
                pinned=pin,
            )
        except NoteStoreError as exc:
            return {"status": exc.code, "message": str(exc)}
        return {
            "status": "ok",
            "slug": page.slug,
            "title": page.title,
            "pinned": page.pinned,
            "message": "Note saved.",
        }

    return upsert_note


def create_read_note_tool(
    note_store: NoteStore,
    is_enabled: bool = True,
):
    """Create a tool that reads one standing note by slug or title."""

    @function_tool(
        name="read_note",
        description=(
            "Read one standing notebook page by slug or title. Use when the catalog "
            "shows a relevant page and you need the current body."
        ),
        param_descriptions={
            "slug_or_title": "Page slug or exact title from the notebook catalog.",
        },
    )
    async def read_note(slug_or_title: str) -> dict:
        """Read a standing note."""
        if not is_enabled:
            return {"status": "disabled", "message": "Notebook reading is disabled for this turn."}
        query = str(slug_or_title or "").strip()
        if not query:
            return {"status": "skipped", "message": "Empty slug_or_title, nothing read."}
        page = await note_store.resolve_page(query, touch=True)
        if page is None:
            return {"status": "not_found", "message": f"No note matched {query!r}."}
        return {
            "status": "ok",
            "slug": page.slug,
            "title": page.title,
            "pinned": page.pinned,
            "updated": page.updated,
            "content": page.body,
        }

    return read_note


def create_list_notes_tool(
    note_store: NoteStore,
    is_enabled: bool = True,
):
    """Create a tool that lists standing notes when the catalog is not enough."""

    @function_tool(
        name="list_notes",
        description=(
            "List standing notebook pages (title, slug, pin, one-line summary). "
            "Prefer the catalog already in context; use this when you need the full list."
        ),
    )
    async def list_notes() -> dict:
        """List standing notes."""
        if not is_enabled:
            return {"status": "disabled", "message": "Notebook reading is disabled for this turn."}
        pages = await note_store.list_pages()
        notes = [
            {
                "slug": page.slug,
                "title": page.title,
                "pinned": page.pinned,
                "updated": page.updated,
                "summary": page.summary,
            }
            for page in pages
        ]
        return {"status": "ok", "count": len(notes), "notes": notes}

    return list_notes


def create_archive_note_tool(
    note_store: NoteStore,
    is_enabled: bool = True,
):
    """Create a tool that archives a standing note."""

    @function_tool(
        name="archive_note",
        description=(
            "Archive a standing notebook page that is stale or wrong. Archived pages "
            "leave the catalog; search can still find them. Prefer archive over leaving "
            "outdated current-version knowledge in the notebook."
        ),
        param_descriptions={
            "slug_or_title": "Page slug or exact title to archive.",
        },
    )
    async def archive_note(slug_or_title: str) -> dict:
        """Archive a standing note."""
        if not is_enabled:
            return {"status": "disabled", "message": "Notebook writing is disabled for this turn."}
        query = str(slug_or_title or "").strip()
        if not query:
            return {"status": "skipped", "message": "Empty slug_or_title, nothing archived."}
        try:
            page = await note_store.archive_page(query)
        except NoteStoreError as exc:
            return {"status": exc.code, "message": str(exc)}
        return {
            "status": "ok",
            "slug": page.slug,
            "title": page.title,
            "message": "Note archived.",
        }

    return archive_note


def create_search_memory_tool(
    memory: MarkdownMemory,
    is_enabled: bool = True,
    message_storage: Optional[MessageStorage] = None,
):
    """Create a tool for searching long-term memory by terms or date range."""

    @function_tool(
        name="search_memory",
        description=(
            "Search older diary memory, standing notes, or raw messages by verbatim terms, date, or date range. "
            "Pass concrete words or short phrases likely to appear in memory; results OR-match "
            "terms and rank by how many terms hit. Prefer recent memory already in context when "
            "present; search when older continuity or facts are needed."
        ),
        param_descriptions={
            "query": (
                "Concrete terms likely to appear in memory "
                "(e.g. [\"Jun\", \"hiking\"], not [\"hobby\", \"interest\", \"pastime\"]). "
                "Leave empty for date-only reads."
            ),
            "date": "Date or range: YYYY-MM-DD or YYYY-MM-DD to YYYY-MM-DD.",
            "scope": "Memory area: daily, weekly, monthly, yearly, notes, or all.",
            "context_lines": "Context lines around each match, default 3.",
        },
    )
    async def search_memory(
        query: Optional[list[str]] = None,
        date: Optional[str] = None,
        scope: str = "all",
        context_lines: int = 3,
    ) -> dict:
        """Search memory files by terms or date. Returns matching text."""
        if not is_enabled:
            return {"results": "", "enabled": False, "message": "Memory reading is disabled for this turn."}

        terms = normalize_terms(query)
        context_lines = max(0, min(int(context_lines), 10))
        max_results = _SEARCH_MAX_RESULTS_WITH_DATE if date else _SEARCH_MAX_RESULTS
        max_chars = _SEARCH_MAX_CHARS
        results = ""

        if terms and not date:
            results = await memory.search_keyword(
                terms=terms,
                scope=scope,
                context_lines=context_lines,
                max_results=max_results,
                max_chars=max_chars,
            )
            if message_storage is not None:
                msg_results = await message_storage.search_messages(
                    terms=terms,
                    max_results=max_results,
                    max_chars=max_chars,
                )
                if msg_results:
                    prefix = "\n\n--- Message Store ---\n" if results else ""
                    results = results + prefix + msg_results
        elif date and not terms:
            if " to " in date:
                parts = date.split(" to ", 1)
                results = await memory.search_date_range(
                    start=parts[0].strip(),
                    end=parts[1].strip(),
                )
            else:
                results = await memory.search_date_range(start=date.strip())
        elif terms and date:
            if " to " in date:
                parts = date.split(" to ", 1)
                date_content = await memory.search_date_range(
                    start=parts[0].strip(),
                    end=parts[1].strip(),
                )
            else:
                date_content = await memory.search_date_range(start=date.strip())
            if date_content:
                lines = date_content.splitlines()
                candidates: list[tuple[int, int, int, str]] = []
                for i, line in enumerate(lines):
                    if score_text(line, terms) <= 0:
                        continue
                    start_idx = max(0, i - context_lines)
                    end_idx = min(len(lines), i + context_lines + 1)
                    block = "\n".join(lines[start_idx:end_idx])
                    candidates.append((score_text(block, terms), start_idx, end_idx, block))
                candidates.sort(key=lambda item: (-item[0], -item[1]))
                covered: set[int] = set()
                matched_blocks: list[str] = []
                used_chars = 0
                for _, start_idx, end_idx, block in candidates:
                    if any(idx in covered for idx in range(start_idx, end_idx)):
                        continue
                    covered.update(range(start_idx, end_idx))
                    separator_len = len("\n---\n") if matched_blocks else 0
                    addition = separator_len + len(block)
                    if matched_blocks and used_chars + addition > max_chars:
                        break
                    if not matched_blocks and len(block) > max_chars:
                        matched_blocks.append(block[:max_chars])
                        break
                    matched_blocks.append(block)
                    used_chars += addition
                    if len(matched_blocks) >= max_results:
                        break
                results = "\n---\n".join(matched_blocks)
            if message_storage is not None:
                if " to " in date:
                    parts = date.split(" to ", 1)
                    msg_results = await message_storage.search_messages(
                        terms=terms,
                        date_start=parts[0].strip(),
                        date_end=parts[1].strip(),
                        max_results=max_results,
                        max_chars=max_chars,
                    )
                else:
                    msg_results = await message_storage.search_messages(
                        terms=terms,
                        date_start=date.strip(),
                        max_results=max_results,
                        max_chars=max_chars,
                    )
                if msg_results:
                    prefix = "\n\n--- Message Store ---\n" if results else ""
                    results = results + prefix + msg_results
        else:
            files = await memory.list_files(scope=scope)
            results = "\n".join(files)

        return {"results": results, "enabled": True}

    return search_memory
