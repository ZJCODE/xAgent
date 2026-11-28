"""Single-track incremental memory projections from committed experience records.

Operational state in ``.memory_projection.json`` holds only ``cursor`` (int) and
``pending`` (crash-safe staged writes). The caller holds the maintenance lock.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from xagent.components.memory import Note, NoteStore, RelationshipCard, RelationshipStore
from xagent.components.memory.note_memory import MAX_BODY_CHARS, MIN_KEY_CHARS
from xagent.core.config import AgentConfig
from xagent.core.inbox import is_scheduled_work
from xagent.core.journal_commits import atomic_write_bytes, read_bytes

logger = logging.getLogger(__name__)

_WIKI = re.compile(r"\[\[\s*(\d{12})\s*\]\]")
_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{1,31}")
_ASCII_STOP_WORDS = frozenset({
    "about", "after", "all", "also", "and", "any", "are", "because", "been",
    "before", "being", "between", "both", "but", "can", "choose", "chosen",
    "could", "did", "does", "doing", "done", "each", "even", "for", "from",
    "get", "got", "had", "has", "have", "having", "her", "here", "him", "his",
    "how", "into", "its", "just", "let", "like", "make", "made", "many", "may",
    "more", "most", "much", "must", "need", "needs", "new", "not", "now", "off",
    "one", "only", "other", "our", "out", "over", "same", "see", "she", "should",
    "some", "still", "such", "take", "than", "that", "the", "their", "them",
    "then", "there", "these", "they", "this", "those", "through", "too", "under",
    "use", "used", "uses", "using", "very", "want", "was", "way", "well", "were",
    "what", "when", "where", "which", "while", "who", "why", "will", "with",
    "without", "would", "you", "your",
})
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_CJK_STOP_SPLIT_RE = re.compile(r"[的了是在有我和你他她它这那不也就都而及与或把被让给到去来从向对为着过吗呢吧啊哦嗯哈]+")
_CONTEXT_CHARS = 6000
_MAX_SEARCH_TERMS = 24
_SEARCH_NOTE_LIMIT = 8
_RECENT_RELATED_LIMIT = 8


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


def _extract_search_terms(text: str) -> list[str]:
    """Extract lightweight ASCII and CJK topic terms for notebook retrieval."""
    seen: set[str] = set()
    terms: list[str] = []

    def add(token: str) -> None:
        folded = token.strip().casefold()
        if len(folded) < MIN_KEY_CHARS or folded in seen:
            return
        seen.add(folded)
        terms.append(token.strip())

    for match in _ASCII_TOKEN_RE.finditer(text or ""):
        token = match.group(0)
        folded = token.casefold()
        if token.isdigit() or folded in _ASCII_STOP_WORDS:
            continue
        if len(token) >= 3 or token.isupper():
            add(token)
        if len(terms) >= _MAX_SEARCH_TERMS:
            return terms

    for run_match in _CJK_RUN_RE.finditer(text or ""):
        for chunk in _CJK_STOP_SPLIT_RE.split(run_match.group(0)):
            if len(chunk) < 2:
                continue
            if len(chunk) <= 6:
                add(chunk)
            else:
                for idx in range(0, len(chunk) - 1, 2):
                    add(chunk[idx : idx + 2])
            if len(terms) >= _MAX_SEARCH_TERMS:
                return terms
    return terms


def _fallback_keys_from_title(title: str) -> list[str]:
    extracted = _extract_search_terms(title)
    if extracted:
        return extracted[:3]
    cleaned = " ".join(str(title or "").split()).strip()
    return [cleaned[:24]] if len(cleaned) >= MIN_KEY_CHARS else []


class MemoryProjector:
    def __init__(
        self,
        root: Path,
        notes: NoteStore,
        relationships: RelationshipStore,
        llm,
        source_chars: int = 24000,
    ):
        self.root = root
        self.path = root / ".memory_projection.json"
        self.notes = notes
        self.relationships = relationships
        self.llm = llm
        self.source_chars = source_chars

    def _load(self) -> dict:
        raw = read_bytes(self.path)
        if not raw:
            return {"cursor": None, "pending": None}
        state = json.loads(raw)
        if not isinstance(state, dict) or set(state) - {"cursor", "pending"}:
            raise ValueError("Invalid memory projection state")
        cursor = state.get("cursor")
        if cursor is not None and (type(cursor) is not int or cursor < 0):
            raise ValueError("Invalid memory projection cursor")
        return {"cursor": cursor, "pending": state.get("pending")}

    async def _save(self, state: dict) -> None:
        content = (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        await _finish_write(asyncio.to_thread(atomic_write_bytes, self.path, content))

    def _projection_files_exist(self) -> bool:
        """True when notes or relationship cards already exist on disk."""
        for dirname in (AgentConfig.NOTES_DIRNAME, AgentConfig.RELATIONSHIPS_DIRNAME):
            directory = self.root / dirname
            if directory.exists() and any(
                path.is_file() and path.stat().st_size > 0
                for path in directory.rglob("*.md")
            ):
                return True
        return False

    async def ensure_initialized(self, processed: int, *, adopt_existing: bool = False) -> None:
        """Initialize a missing projection cursor without rebuilding copied memory."""
        state = await asyncio.to_thread(self._load)
        if type(state["cursor"]) is int:
            return
        if adopt_existing or self._projection_files_exist():
            logger.warning(
                "Projection cursor missing while established memory exists; "
                "seeding cursor to %d without rebuilding projections",
                processed,
            )
            cursor = processed
        else:
            cursor = 0
        await self._save({"cursor": cursor, "pending": None})

    async def _context(self, text: str, people: list[dict]):
        cards: dict[str, str] = {}
        kept_people: list[dict] = []
        used = 0
        existing_cards = {
            card.key: card
            for card in await self.relationships.read_cards([person["key"] for person in people])
        }
        for person in people:
            card = existing_cards.get(person["key"])
            size = len(card.body) if card else 0
            if used + size > _CONTEXT_CHARS:
                continue
            kept_people.append(person)
            if card:
                cards[card.key] = card.body
            used += size

        # Dual-path note retrieval: exact trigger keys + topic term search over title/body.
        recalled = await self.notes.recall(text, limit=None)
        terms = _extract_search_terms(text)
        searched = await self.notes.search(terms, limit=_SEARCH_NOTE_LIMIT) if terms else []
        seen_candidates: set[str] = set()
        candidate_notes: list[Note] = []
        for note in [*recalled, *searched]:
            if note.id not in seen_candidates:
                seen_candidates.add(note.id)
                candidate_notes.append(note)

        existing: list[dict] = []
        related: list[dict] = []
        allowed: set[str] = set()
        for note in candidate_notes:
            record = note.to_dict()
            size = len(json.dumps(record, ensure_ascii=False))
            if used + size > _CONTEXT_CHARS:
                continue
            existing.append(record)
            allowed.add(note.id)
            used += size

        summary_candidates: list[Note] = []
        for record in existing:
            note = await self.notes.read(record["id"])
            if note is not None:
                summary_candidates.extend(await self.notes.neighbours(note, limit=3))
        summary_candidates.extend(await self.notes.search([], limit=_RECENT_RELATED_LIMIT))

        for item in summary_candidates:
            if item.id in allowed:
                continue
            summary = {
                "id": item.id,
                "title": item.title,
                "snippet": item.snippet[: AgentConfig.NOTEBOOK_SNIPPET_MAX_CHARS],
            }
            size = len(json.dumps(summary, ensure_ascii=False))
            if used + size <= _CONTEXT_CHARS:
                related.append(summary)
                allowed.add(item.id)
                used += size

        return kept_people, cards, existing, related, allowed

    async def project_messages(self, handler, processed: int) -> bool:
        state = await asyncio.to_thread(self._load)
        if state["cursor"] is None:
            await self.ensure_initialized(processed)
            state = await asyncio.to_thread(self._load)
        if state["pending"] is not None:
            await _finish_write(self._apply_pending(state))
        start = state["cursor"]
        end = min(processed, start + handler.diary_write_batch)
        if start >= end:
            return False

        messages = await handler.message_storage.get_messages_in_cursor_range(start, end)
        records: list[dict] = []
        people: dict[str, dict] = {}
        size = 0
        for message in messages:
            if not handler._is_memory_worthy_experience(message):
                continue
            record_id = message.metadata[AgentConfig.MESSAGE_STORAGE_CURSOR_KEY]
            metadata = message.metadata or {}
            source = metadata.get("source")
            record_type = message.type.value
            if is_scheduled_work(metadata):
                record_type = "scheduled_task"
            elif message.role.value == "assistant" and source == "subconscious":
                record_type = "private_reflection"
            record = {
                "id": record_id,
                "role": message.role.value,
                "type": record_type,
                "source": source,
                "channel": message.channel,
                "room": message.room_name,
                "timestamp": message.timestamp,
                "content": message.content,
            }
            speakers = handler._extract_participants([message])
            if speakers:
                speaker = speakers[0]
                key = self.relationships.primary_key(speaker["key"])
                record["person"] = key
                record["sender"] = speaker["key"]
                channel, user_id = self.relationships.split_key(key)
                speaker = dict(speaker, key=key, channel=channel, user_id=user_id)
            cost = len(json.dumps(record, ensure_ascii=False))
            if records and size + cost > self.source_chars:
                end = record_id - 1
                break
            if speakers:
                existing_person = people.get(key)
                if existing_person and not speaker.get("display_name"):
                    speaker["display_name"] = existing_person.get("display_name", "")
                people[key] = speaker
            records.append(record)
            size += cost

        if not records:
            state["cursor"] = end
            await self._save(state)
            return end < processed

        text = "\n".join(r["content"] for r in records)
        persons, cards, notes, related, allowed = await self._context(text, list(people.values()))
        editable = {n["id"] for n in notes}
        inputs = dict(
            messages=records,
            participants=persons,
            existing_cards=cards,
            existing_notes=notes,
            related_notes=related,
        )
        day = datetime.fromtimestamp(records[-1]["timestamp"]).date().isoformat()
        pending = {"cursor": end, "relationships": [], "notes": []}
        issues: list[str] = []
        for attempt in range(2):
            changes = await self.llm.update_memory_projections(**inputs)
            pending, issues = await self._prepare(
                changes, persons, editable, allowed, day, end, records,
            )
            if not issues:
                break
            if attempt == 0:
                inputs["correction"] = "; ".join(issues)
            else:
                summary = "; ".join(issues)
                logger.warning("Dropped invalid projection items after retry: %s", summary)
                if hasattr(handler, "_last_maintenance") and isinstance(handler._last_maintenance, dict):
                    handler._last_maintenance["projection_error"] = summary

        state["pending"] = pending
        await self._save(state)
        await _finish_write(self._apply_pending(state))
        return end < processed

    async def _prepare(
        self,
        changes: object,
        people: list[dict],
        editable: set[str],
        allowed: set[str],
        journal_date: str,
        through: int,
        records: list[dict],
    ) -> tuple[dict, list[str]]:
        writes = {"cursor": through, "relationships": [], "notes": []}
        issues: list[str] = []
        if not isinstance(changes, dict) or set(changes) - {"relationships", "notes"}:
            return writes, ["Expected top-level object with only `relationships` and `notes`."]
        cards = changes.get("relationships", {})
        proposals = changes.get("notes", [])
        if not isinstance(cards, dict) or not isinstance(proposals, list):
            return writes, ["`relationships` must be an object and `notes` must be an array."]

        people_by_key = {person["key"]: person for person in people}
        all_record_ids = {r["id"] for r in records}

        def resolve_evidence(change: dict, existing_item: Optional[object], person_key: Optional[str] = None) -> tuple[Optional[dict], Optional[str]]:
            refs = change.get("messages")
            if not isinstance(refs, list) or not refs or not all(type(i) is int and i in all_record_ids for i in refs):
                return None, (
                    f"Invalid `messages` for {person_key or 'note'}: cite valid IDs from {sorted(all_record_ids)}."
                )
            if person_key is not None:
                person_ids = {r["id"] for r in records if r.get("person") == person_key}
                if not any(i in person_ids for i in refs):
                    return None, (
                        f"Relationship `{person_key}` must include at least one message from {sorted(person_ids)} "
                        "(you may also include surrounding context IDs)."
                    )
            source = dict(existing_item.source) if existing_item else {}
            source["messages"] = sorted({*source.get("messages", []), *refs})
            return source, None

        for key, change in cards.items():
            if key not in people_by_key:
                issues.append(f"Unknown relationship key `{key}` (allowed: {sorted(people_by_key)}).")
                continue
            if not isinstance(change, dict) or set(change) != {"body", "messages"}:
                issues.append(f"Relationship `{key}` must be `{{\"body\": string|null, \"messages\": [int]}}`.")
                continue
            body = change["body"]
            if body is not None and (not isinstance(body, str) or not body.strip()):
                issues.append(f"Relationship `{key}` body must be non-empty text or null.")
                continue
            if isinstance(body, str) and len(body.strip()) > MAX_BODY_CHARS:
                issues.append(
                    f"Relationship `{key}` exceeds {MAX_BODY_CHARS} chars ({len(body.strip())}); condense it."
                )
                continue
            existing_card = await self.relationships.read_card(key)
            source, err = resolve_evidence(change, existing_card, person_key=key)
            if err:
                issues.append(err)
                continue
            if body is None:
                if existing_card:
                    writes["relationships"].append({"key": key, "body": None})
                continue
            body = body.strip()
            if existing_card and existing_card.body == body:
                continue
            person = people_by_key[key]
            display_name = person.get("display_name") or (existing_card.display_name if existing_card else "")
            writes["relationships"].append({
                "key": key,
                "body": body,
                "display_name": display_name,
                "channel": person["channel"],
                "user_id": person["user_id"],
                "updated": journal_date,
                "source": source,
            })

        all_notes = {note.id: note for note in await self.notes.list_notes()}
        reserved = set(all_notes)
        seen: set[str] = set()
        deleted = {
            p.get("id")
            for p in proposals
            if isinstance(p, dict) and p.get("body", "") is None and isinstance(p.get("id"), str)
        }
        for proposal in proposals:
            if not isinstance(proposal, dict) or set(proposal) - {"id", "title", "body", "keys", "links", "messages"}:
                issues.append("Each note must contain only `id`, `title`, `body`, `keys`, `links`, `messages`.")
                continue
            note_id = proposal.get("id")
            if note_id is not None and (not isinstance(note_id, str) or note_id not in editable or note_id in seen):
                issues.append(
                    f"Unknown, non-editable, or repeated note id `{note_id}` (editable: {sorted(editable)})."
                )
                continue
            existing_note = all_notes.get(note_id) if note_id else None
            if proposal.get("body", "") is None:
                if not existing_note:
                    issues.append("Cannot delete a note without a valid existing `id`.")
                    continue
                _, err = resolve_evidence(proposal, existing_note)
                if err:
                    issues.append(err)
                    continue
                seen.add(note_id)
                writes["notes"].append({"id": note_id, "body": None})
                continue

            source, err = resolve_evidence(proposal, existing_note)
            if err:
                issues.append(err)
                continue
            title = proposal.get("title")
            body = proposal.get("body")
            raw_keys = proposal.get("keys", [])
            links = proposal.get("links", [])
            if not isinstance(title, str) or not title.strip():
                issues.append("Note `title` must be non-empty text.")
                continue
            if not isinstance(body, str) or not body.strip():
                issues.append("Note `body` must be non-empty text or null.")
                continue
            if len(body.strip()) > MAX_BODY_CHARS:
                issues.append(
                    f"Note `{title[:30]}` exceeds {MAX_BODY_CHARS} chars ({len(body.strip())}); condense it."
                )
                continue
            if not isinstance(raw_keys, list) or not all(isinstance(k, str) for k in raw_keys):
                issues.append("Note `keys` must be an array of strings.")
                continue
            if not isinstance(links, list) or not all(isinstance(link, str) for link in links):
                issues.append("Note `links` must be an array of note ID strings.")
                continue
            cleaned_keys = [k.strip() for k in raw_keys if len(k.strip()) >= MIN_KEY_CHARS]
            if not cleaned_keys:
                cleaned_keys = _fallback_keys_from_title(title)
            if not cleaned_keys:
                issues.append(f"Note `{title}` needs at least one trigger key of >= {MIN_KEY_CHARS} chars.")
                continue

            if note_id:
                seen.add(note_id)
            else:
                stamp = datetime.strptime(self.notes.next_id(), "%Y%m%d%H%M")
                while stamp.strftime("%Y%m%d%H%M") in reserved:
                    stamp += timedelta(minutes=1)
                note_id = stamp.strftime("%Y%m%d%H%M")
                reserved.add(note_id)

            targets = (allowed - deleted) - {note_id}
            cleaned_body = _WIKI.sub(
                lambda match: match.group(0) if match.group(1) in targets else match.group(1),
                body,
            )
            candidate = NoteStore.normalize(Note(
                id=note_id,
                title=title,
                body=cleaned_body,
                keys=tuple(cleaned_keys),
                links=tuple(link for link in [*links, *_WIKI.findall(cleaned_body)] if link in targets),
                source=source,
                updated=journal_date,
            ))

            def content(note: Note):
                return (
                    note.title,
                    note.body,
                    frozenset(key.casefold() for key in note.keys),
                    frozenset(note.links),
                )

            if existing_note and content(existing_note) == content(candidate):
                continue
            if not existing_note and any(
                (other.title.casefold(), other.body) == (candidate.title.casefold(), candidate.body)
                for other in all_notes.values()
            ):
                continue
            writes["notes"].append(candidate.to_dict())
            all_notes[note_id] = candidate

        return writes, issues

    async def _apply_pending(self, state: dict) -> None:
        pending = state["pending"]
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
