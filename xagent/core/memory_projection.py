"""Incremental diary projections with one durable cursor and pending result.

This is operational recovery state, not another memory store. Diary bytes are
read from their source; only identity metadata and already generated writes
are retained here. The caller holds the diary maintenance/process lock.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from xagent.components.memory import Note, NoteStore, RelationshipCard, RelationshipStore
from xagent.components.memory.note_memory import (
    MAX_BODY_CHARS, MAX_KEYS, MAX_TITLE_CHARS, MIN_KEY_CHARS,
)
from xagent.core.config import AgentConfig
from xagent.core.journal_commits import atomic_write_bytes, digest, read_bytes

_ENTRY = re.compile(r"(?m)^## \d{4}-\d{2}-\d{2} \d{2}:\d{2}\s*$")
_WIKI = re.compile(r"\[\[\s*(\d{12})\s*\]\]")
_CONTEXT_CHARS = 6000


async def _finish_write(operation):
    """Keep the caller's lock until cancelled disk workers have stopped."""
    task = asyncio.create_task(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        task.result()
        raise


class MemoryProjector:
    def __init__(self, root: Path, notes: NoteStore, relationships: RelationshipStore, llm,
                 source_chars: int = 24000):
        self.root = root
        self.path = root / ".memory_projection.json"
        self.notes = notes
        self.relationships = relationships
        self.llm = llm
        self.source_chars = source_chars

    def _load(self) -> dict:
        raw = read_bytes(self.path)
        if not raw:
            return {"cursor": None, "people": {}, "pending": None}
        state = json.loads(raw)
        if not isinstance(state, dict) or set(state) != {"cursor", "people", "pending"}:
            raise ValueError("Invalid memory projection state")
        if not isinstance(state["people"], dict):
            raise ValueError("Invalid projection identities")
        return state

    async def _save(self, state: dict) -> None:
        content = (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        await _finish_write(asyncio.to_thread(atomic_write_bytes, self.path, content))

    async def remember_people(self, participants: list[dict]) -> None:
        """Persist routing metadata before journaling; never persist their claims."""
        state = await asyncio.to_thread(self._load)
        changed = False
        for person in participants:
            key = person["key"]
            previous = state["people"].get(key, {})
            updated = {**person}
            if not updated.get("display_name"):
                updated["display_name"] = previous.get("display_name", "")
            updated["aliases"] = sorted({
                *previous.get("aliases", []),
                *filter(None, [previous.get("display_name"), updated.get("display_name")]),
            })
            if previous != updated:
                state["people"][key] = updated
                changed = True
        if changed:
            await self._save(state)

    def _verify_cursor(self, cursor: dict) -> bytes:
        path = self.root / cursor["path"]
        if not path.resolve().is_relative_to((self.root / "daily").resolve()):
            raise ValueError("Projection cursor escapes the daily diary")
        content = read_bytes(path)
        offset = cursor["offset"]
        if not isinstance(offset, int) or offset < 0 or len(content) < offset:
            raise ValueError("Diary was shortened behind the projection cursor")
        if digest(content[:offset]) != cursor["sha256"]:
            raise ValueError("Diary changed behind the projection cursor")
        return content

    def _next_source(self, cursor: dict | None):
        if cursor:
            self._verify_cursor(cursor)
        for path in sorted((self.root / "daily").rglob("*.md")):
            relative = path.relative_to(self.root).as_posix()
            if cursor and relative < cursor["path"]:
                continue
            offset = cursor["offset"] if cursor and relative == cursor["path"] else 0
            content = read_bytes(path)
            if len(content) <= offset:
                continue
            journal_date = date.fromisoformat(path.stem).isoformat()
            tail = content[offset:].decode("utf-8")
            end = len(tail)
            if end > self.source_chars:
                boundaries = [m.start() for m in _ENTRY.finditer(tail)
                              if m.start() > 0 and tail[:m.start()].strip()]
                fits = [position for position in boundaries if position <= self.source_chars]
                end = max(fits) if fits else (boundaries[0] if boundaries else end)
            text = tail[:end]
            next_offset = offset + len(text.encode("utf-8"))
            through = {"path": relative, "offset": next_offset,
                       "sha256": digest(content[:next_offset])}
            return text, journal_date, through
        return None

    @staticmethod
    def _participants(people: dict, text: str) -> list[dict]:
        names: dict[str, int] = {}
        for person in people.values():
            for name in {str(value).casefold() for value in
                         [person.get("display_name", ""), *person.get("aliases", [])] if value}:
                names[name] = names.get(name, 0) + 1
        selected = []
        text = text.casefold()
        for person in people.values():
            aliases = {str(value).casefold() for value in
                       [person.get("display_name", ""), *person.get("aliases", [])] if value}
            user_id = str(person.get("user_id") or "").casefold()
            explicit_id = (person["key"].casefold() in text or (
                len(user_id) >= 2 and names.get(user_id, 0) <= 1 and
                re.search(r"(?<![a-z0-9_])" + re.escape(user_id) + r"(?![a-z0-9_])", text)
            ))
            if explicit_id or any(names[name] == 1 and name in text for name in aliases):
                selected.append(person)
        return selected

    async def _context(self, state: dict, text: str):
        people = self._participants(state["people"], text)
        cards = {}
        kept_people = []
        used = 0
        existing_cards = {card.key: card for card in await self.relationships.read_cards(
            [person["key"] for person in people]
        )}
        for person in people:
            card = existing_cards.get(person["key"])
            size = len(card.body) if card else 0
            if used + size > _CONTEXT_CHARS:
                continue
            kept_people.append(person)
            if card:
                cards[card.key] = card.body
            used += size
        people = kept_people
        notes = await self.notes.recall(text, limit=AgentConfig.NOTEBOOK_RECALL_MAX)
        existing, related, allowed = [], [], set()
        # Whole cards only, under one fixed context budget. One-hop summaries
        # contribute ids for meaningful references without recursive expansion.
        for note in notes:
            record = note.to_dict()
            size = len(json.dumps(record, ensure_ascii=False))
            if used + size > _CONTEXT_CHARS:
                continue
            existing.append(record)
            allowed.add(note.id)
            used += size
        for record in existing:
            note = await self.notes.read(record["id"])
            for neighbour in await self.notes.neighbours(note, limit=3):
                if neighbour.id in allowed:
                    continue
                summary = {"id": neighbour.id, "title": neighbour.title,
                           "snippet": neighbour.snippet[:AgentConfig.NOTEBOOK_SNIPPET_MAX_CHARS]}
                size = len(json.dumps(summary, ensure_ascii=False))
                if used + size <= _CONTEXT_CHARS:
                    related.append(summary)
                    allowed.add(neighbour.id)
                    used += size
        return people, cards, existing, related, allowed

    async def run(self) -> bool:
        state = await asyncio.to_thread(self._load)
        if state["pending"] is not None:
            await _finish_write(self._apply_pending(state))
            return True
        source = await asyncio.to_thread(self._next_source, state["cursor"])
        if source is None:
            return False
        text, journal_date, through = source
        if not text.strip():
            state["cursor"] = through
            await self._save(state)
            return True
        people, cards, notes, related, allowed = await self._context(state, text)
        changes = await self.llm.update_memory_projections(
            committed_diary=text, participants=people, existing_cards=cards,
            existing_notes=notes, related_notes=related,
        )
        pending = await self._prepare(
            changes, people, {note["id"] for note in notes}, allowed, journal_date, through,
        )
        # Persist assigned ids and exact writes BEFORE applying any projection.
        # A restart replays these writes, not the model call or diary append.
        state["pending"] = pending
        await self._save(state)
        await _finish_write(self._apply_pending(state))
        return True

    async def _prepare(self, changes, people, editable, allowed, journal_date, through):
        if not isinstance(changes, dict) or set(changes) - {"relationships", "notes"}:
            raise ValueError("Invalid memory projection result")
        cards = changes.get("relationships", {})
        proposals = changes.get("notes", [])
        if not isinstance(cards, dict) or not isinstance(proposals, list):
            raise ValueError("Invalid memory projection fields")
        people_by_key = {person["key"]: person for person in people}
        writes = {"cursor": through, "relationships": [], "notes": []}
        for key, body in cards.items():
            if key not in people_by_key or (body is not None and
                    (not isinstance(body, str) or not body.strip() or len(body) > MAX_BODY_CHARS)):
                raise ValueError("Invalid relationship change")
            existing = await self.relationships.read_card(key)
            if body is None:
                if existing:
                    writes["relationships"].append({"key": key, "body": None})
                continue
            body = body.strip()
            if existing and existing.body == body:
                continue
            person = people_by_key[key]
            sources = sorted({*(existing.source.get("diary", []) if existing else []), journal_date})
            writes["relationships"].append({
                "key": key, "body": body, "display_name": person.get("display_name", ""),
                "channel": person["channel"], "user_id": person["user_id"],
                "updated": journal_date, "source": {"diary": sources},
            })
        all_notes = {note.id: note for note in await self.notes.list_notes()}
        reserved = set(all_notes)
        seen = set()
        deleted = {p.get("id") for p in proposals if isinstance(p, dict) and p.get("body", "") is None}
        for proposal in proposals:
            if not isinstance(proposal, dict) or set(proposal) - {"id", "title", "body", "keys", "links"}:
                raise ValueError("Invalid note change")
            note_id = proposal.get("id")
            if note_id is not None and (not isinstance(note_id, str) or note_id not in editable or note_id in seen):
                raise ValueError("Unknown or repeated note id")
            existing = all_notes.get(note_id)
            if note_id:
                seen.add(note_id)
            if proposal.get("body", "") is None:
                if existing:
                    writes["notes"].append({"id": note_id, "body": None})
                else:
                    raise ValueError("Cannot delete a new note")
                continue
            title, body = proposal.get("title"), proposal.get("body")
            keys, links = proposal.get("keys", []), proposal.get("links", [])
            if (not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE_CHARS or
                    not isinstance(body, str) or not body.strip() or len(body) > MAX_BODY_CHARS or
                    not isinstance(keys, list) or not 1 <= len(keys) <= MAX_KEYS or
                    not all(isinstance(key, str) and len(key.strip()) >= MIN_KEY_CHARS for key in keys) or
                    not isinstance(links, list) or not all(isinstance(link, str) for link in links)):
                raise ValueError("Invalid note content")
            if note_id is None:
                stamp = datetime.strptime(self.notes.next_id(), "%Y%m%d%H%M")
                while stamp.strftime("%Y%m%d%H%M") in reserved:
                    stamp += timedelta(minutes=1)
                note_id = stamp.strftime("%Y%m%d%H%M")
                reserved.add(note_id)
            targets = (allowed - deleted) - {note_id}
            body = _WIKI.sub(lambda match: match.group(0) if match.group(1) in targets else match.group(1), body)
            candidate = NoteStore.normalize(Note(
                id=note_id, title=title, body=body, keys=tuple(keys),
                links=tuple(link for link in [*links, *_WIKI.findall(body)] if link in targets),
                source={"diary": sorted({*(existing.source.get("diary", []) if existing else []), journal_date})},
                updated=journal_date,
            ))
            def content(note):
                return (note.title, note.body, frozenset(key.casefold() for key in note.keys),
                        frozenset(note.links))

            if existing and content(existing) == content(candidate):
                continue
            if not existing and any((other.title.casefold(), other.body) ==
                                    (candidate.title.casefold(), candidate.body)
                                    for other in all_notes.values()):
                continue
            writes["notes"].append(candidate.to_dict())
            all_notes[note_id] = candidate
        return writes

    async def _apply_pending(self, state):
        pending = state["pending"]
        await asyncio.to_thread(self._verify_cursor, pending["cursor"])
        for record in pending["relationships"]:
            if record["body"] is None:
                await self.relationships.delete_card(record["key"])
            else:
                await self.relationships.write_card(RelationshipCard(**record))
        for record in pending["notes"]:
            if record["body"] is None:
                await self.notes.delete(record["id"])
            else:
                await self.notes.write(Note(**record))
        state["cursor"] = pending["cursor"]
        state["pending"] = None
        await self._save(state)
