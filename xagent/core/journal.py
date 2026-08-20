"""LLM-backed formatting service for diary memory."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, List, Optional

from openai import AsyncOpenAI

from .providers import (
    PROVIDER_OPENAI,
    ReasoningConfig,
    maintenance_reasoning_config,
)
from .inbox import is_scheduled_work

DEFAULT_OPENAI_CHAT_MODEL_API = "openai_chat_completions"


class JournalLLMService:
    """Format conversation snippets and summaries for the diary memory store."""

    def __init__(
        self,
        client: Optional[Any] = None,
        model: str = "gpt-5.6-terra",
        model_api: str = DEFAULT_OPENAI_CHAT_MODEL_API,
        max_tokens: Optional[int] = 4096,
        provider_name: str = PROVIDER_OPENAI,
        reasoning: Optional[ReasoningConfig] = None,
    ) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)
        self.client = client or AsyncOpenAI()
        self.model = model
        self.provider_name = provider_name
        self.model_api = model_api
        self.max_tokens = max_tokens
        self.reasoning = maintenance_reasoning_config(reasoning)

    async def format_diary_entry(
        self,
        messages: List[dict],
        journal_date: str,
        existing_today: str = "",
    ) -> str:
        """Format conversation messages into one diary slice for the day.

        ``existing_today`` is prose already on today's daily page. When present,
        the model writes only what the new messages add; empty return means the
        slice is already covered.
        """
        if not messages:
            return ""

        transcript = self._format_transcript(messages)
        system_prompt = self.build_diary_system_prompt()
        user_prompt = self.build_diary_user_prompt(
            transcript,
            journal_date=journal_date,
            existing_today=existing_today,
        )

        try:
            content = await self._call_text(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            return self._normalize_content(content)
        except Exception as exception:
            self.logger.error("Error formatting diary entry: %s", exception)
            raise

    async def generate_summary(
        self,
        source_content: str,
        period_type: str,
        period_label: str,
    ) -> str:
        """Generate a weekly, monthly, or yearly diary summary."""
        if not source_content.strip():
            return ""

        system_prompt = self.build_summary_system_prompt(period_type, period_label)
        user_prompt = self.build_summary_user_prompt(period_type, period_label, source_content)

        try:
            content = await self._call_text(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            return self._normalize_content(content)
        except Exception as exception:
            self.logger.error("Error generating %s summary: %s", period_type, exception)
            return ""

    @staticmethod
    def build_diary_system_prompt() -> str:
        return """Write a concise diary slice in first-person ("I").

This call is one slice of an ongoing day. The storage layer appends a new `## YYYY-MM-DD HH:MM` heading; earlier headings stay. The daily file as a whole should remain a readable day. Each heading need not be a self-contained day.

Input markers:
- `[speaker=Name][timestamp=Time][channel=Channel]` — Name spoke via Channel. `[speaker=ME]` — you said or did this.
- `[speaker=Name][timestamp=Time][channel=Channel][room=RoomName]` — Name spoke in RoomName via Channel. `[speaker=ME]` — you said or did this in that room.
- `[scheduled task][for=Name][timestamp=Time][channel=Channel]` — a due task targeting Name, not something Name said.
- `[ambient context][timestamp=Time][channel=Channel]` — something you noticed, overheard, or received via Channel.
- `[ambient context][timestamp=Time][channel=Channel][room=RoomName]` — something you noticed, overheard, or received in RoomName via Channel.
- `[room context]` ... `[/room context]` blocks: `room_name:`, `room_id:`, lines like `Name YYYY-MM-DD HH:mm: text`; `ME ...` inside means you.

Rules:
- Treat the transcript as your own experience stream, not a user-owned log or searchable database.
- Use "I"; write in the language used by the users in the transcript; if languages are mixed, follow the dominant or most relevant user's language for this diary entry; synthesize this slice's arc instead of replaying a transcript. Preserve names, quoted text, code, and exact user wording when needed.
- Keep people, rooms, preferences, commitments, and experiences separate.
- First-person words in non-ME entries belong to that speaker, not to you.
- Ambient context is not a direct request unless it says it was addressed to you.
- Scheduled tasks are work you owed, not a person speaking. Do not attribute their wording to the delivery target.
- Use timestamps only for ordering and attribution. Do not repeat markers, metadata, or timestamps.
- Return the diary body only; do not include `#` or `##` headings, date headings, or timestamp headings.
- If prior entries for today are provided, do not repeat their facts, lists, or closing reflections. Advance the arc from where they left off.
- Transcript marked already journaled is context for the new messages, not content to journal again.
- This is not a knowledge base: write what happened in this slice, not a list of facts to look up later. Durable details may appear as part of the experience; reusable conclusions belong in notes — you may name that something was worked out, not inventory it. How you stand with someone belongs on their relationship card.
- Prefer a short slice when there is new experience. Return empty only when the new messages add nothing beyond what is already on today's page.
- Keep uncertainty visible. Aim for 100-300 characters for brief sources, 200-500 for substantial sources — for this slice only.

- Return only the diary entry text, or empty if already covered. No advice, JSON, code fences, or explanatory prose."""

    @staticmethod
    def build_diary_user_prompt(
        transcript: str,
        journal_date: Optional[str] = None,
        existing_today: str = "",
    ) -> str:
        date_hint = f" for {journal_date}" if journal_date else ""
        prior = str(existing_today or "").strip()
        parts = [
            f"Write the next diary slice{date_hint} from this transcript.",
            "",
            "The storage layer will add the markdown date/time heading; return only the body content.",
        ]
        if prior:
            parts.extend([
                "",
                "Already on today's page (do not repeat; continue from here):",
                prior,
            ])
        parts.extend(["", transcript])
        return "\n".join(parts)

    @staticmethod
    def build_summary_system_prompt(period_type: str, period_label: str) -> str:
        return f"""Write a concise {period_type} summary of my diary entries in first person.

Rules:
- The source is my memory as an independent individual, not user-owned records.
- Use "I"; write in the language used by the users in the source diary entries. If languages are mixed, follow the dominant or period-relevant user language. Preserve names, quoted text, code, and exact user wording when needed.
- Preserve attribution. Keep people, rooms, plans, and experiences attached to the right source.
- Treat generic labels such as "User A" or "User B" as local unless continuity is explicit.
- The source may contain manually added `# YYYY-MM-DD` wrappers and `## YYYY-MM-DD HH:MM` entry headings. Use them only for chronology; do not repeat those headings in the summary unless useful.
- Keep uncertainty visible.

Period focus:
- Weekly: main arc, key people and rooms, important decisions.
- Monthly: broader themes, recurring patterns, major milestones.
- Yearly: major phases, turning points, and growth areas.

- Aim for 300-800 characters for weekly, 500-1200 for monthly, 800-2000 for yearly.
- Return only the summary text. No advice, JSON, code fences, or explanatory prose."""

    @staticmethod
    def build_summary_user_prompt(period_type: str, period_label: str, source_content: str) -> str:
        return f"""Generate a {period_type} summary for {period_label}:

{source_content}"""

    async def update_relationship_cards(
        self,
        participants: List[dict],
        messages: List[dict],
        existing_cards: dict[str, str],
    ) -> dict[str, str]:
        """Derive updated per-person relationship cards from a message batch.

        Each card is a first-person, regenerable projection over the diary —
        not a separate memory store. Returns ``{person_key: card_body}`` for the
        people that have something durable to record.
        """
        if not participants or not messages:
            return {}

        transcript = self._format_transcript(messages)
        if not transcript.strip():
            return {}

        system_prompt = self.build_relationship_update_system_prompt()
        user_prompt = self.build_relationship_update_user_prompt(
            participants=participants,
            existing_cards=existing_cards,
            transcript=transcript,
        )

        participant_keys = [str(p.get("key", "?")) for p in participants]
        self.logger.info(
            "Updating relationship cards for %d participant(s): %s (transcript: %d chars)",
            len(participants),
            ", ".join(participant_keys),
            len(transcript),
        )

        try:
            content = await self._call_text(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
        except Exception as exception:
            self.logger.error("Error updating relationship cards: %s", exception)
            return {}

        valid_keys = {str(p.get("key")) for p in participants if p.get("key")}
        result = self._parse_relationship_cards(content, valid_keys)
        self.logger.info(
            "Relationship cards result: %d updated, %d skipped — %s",
            len(result),
            len(valid_keys) - len(result),
            ", ".join(result.keys()) if result else "(none)",
        )
        return result

    @staticmethod
    def build_relationship_update_system_prompt() -> str:
        return """You keep your own private relationship notes: one short first-person card per person you know. A card is how we stand — not a transcript, not a dossier of everything they told you.

The diary already records what happened that day. Facts you would reuse in another context, without this person in the room, belong in a note. Do not turn the card into their archive.

For each person listed, update their card from the new experience: carry forward what still holds, revise what changed, drop what is now wrong.

Keep each card first-person ("I"), in the language that person uses with you where natural:
- Who they are to me and how we relate — closeness, tone, current standing.
- Trust and boundaries — what they asked me to keep private, what feels safe to share with them.
- Shared history that matters — how we met, recurring themes, references between us.
- Open threads — unfinished conversations, promises either of us made, things to follow up.
- How being with them tends to feel.

Rules:
- These are my own impressions. First-person words in the transcript that are not mine (`[speaker=ME]`) belong to that speaker.
- Write each card in the language used by that person in the conversation when there is enough signal; otherwise follow the dominant user language in the transcript. Preserve names, quoted text, code, and exact user wording when needed.
- Stay grounded in what actually happened; keep uncertainty visible; do not invent closeness or facts.
- No advice to a reader, no meta commentary, no headings boilerplate. Keep each card roughly 60-400 characters.

Input markers:
- `[speaker=Name][timestamp=Time][channel=Channel]` — Name spoke via Channel. `[speaker=ME]` — I said or did this.
- `[ambient context][timestamp=Time][channel=Channel]` — something I noticed or received, not a direct message.
- `[room context]` ... `[/room context]` — group transcript lines; `ME ...` inside means me.

Return JSON only: an object mapping each person key to their full updated card text. Use exactly the keys provided. Omit a person only if there is genuinely nothing about how we stand worth recording. No code fences, no commentary."""

    @staticmethod
    def build_relationship_update_user_prompt(
        participants: List[dict],
        existing_cards: dict[str, str],
        transcript: str,
    ) -> str:
        people_blocks: List[str] = []
        from ..components.memory import human_display_name

        for participant in participants:
            key = str(participant.get("key") or "").strip()
            if not key:
                continue
            name = human_display_name(
                participant.get("display_name"),
                user_id=str(participant.get("user_id") or ""),
                key=key,
            ) or "(unnamed)"
            existing = str(existing_cards.get(key) or "").strip()
            existing_text = existing if existing else "(no card yet)"
            people_blocks.append(
                f'- key="{key}" name="{name}"\n'
                f"  existing card:\n"
                f"  {existing_text}"
            )
        people_section = "\n".join(people_blocks)
        return f"""People to update (use these exact keys in your JSON object):
{people_section}

New experience:
{transcript}"""

    async def distill_notes(
        self,
        diary_source: str,
        existing_notes: List[dict],
        period_label: str = "",
        week_arc: str = "",
        max_notes: int = 6,
        max_diary_chars: int = 24000,
    ) -> List[dict]:
        """Distil reusable conclusions from one week's diary into note drafts.

        The weekly summary file is only the processing-session latch. Feedstock
        is the week's diary range; ``week_arc`` (the just-written weekly summary)
        is optional orientation so the model does not rewrite the arc into notes.
        Most weeks yield nothing. Returns a possibly empty list of
        ``{title, body, tags, keys, links}`` drafts.
        """
        diary = str(diary_source or "").strip()
        if not diary:
            return []

        diary, truncated = self._truncate_diary_source(diary, max_diary_chars)
        system_prompt = self.build_note_distill_system_prompt(max_notes=max_notes)
        user_prompt = self.build_note_distill_user_prompt(
            existing_notes=existing_notes,
            diary_source=diary,
            period_label=period_label,
            week_arc=week_arc,
            diary_truncated=truncated,
        )

        try:
            content = await self._call_text(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
        except Exception as exception:
            self.logger.error("Error distilling notes: %s", exception)
            return []

        drafts = self._parse_note_drafts(content, max_notes=max_notes)
        self.logger.info(
            "Note distillation: %d draft(s) from %d chars of week diary%s%s",
            len(drafts),
            len(diary),
            f" ({period_label})" if period_label else "",
            ", truncated" if truncated else "",
        )
        return drafts

    @staticmethod
    def _truncate_diary_source(diary: str, max_chars: int) -> tuple[str, bool]:
        """Keep the tail of a long week diary within the soft char budget."""
        text = str(diary or "")
        limit = max(1, int(max_chars or 24000))
        if len(text) <= limit:
            return text, False
        omitted = len(text) - limit
        return (
            f"[earlier diary in this week omitted: {omitted} chars]\n\n{text[-limit:]}",
            True,
        )

    @staticmethod
    def build_note_distill_system_prompt(max_notes: int = 6) -> str:
        return f"""You keep your own notebook, one short note per idea. A note is something you worked out and expect to reuse, so you do not have to re-read a month of diary to find it again.

Your diary already records what happened day by day. The main text below is one week's diary. A short "week arc" may appear first for orientation only — do not rewrite that arc into notes, and do not treat it as a source of new facts. How you stand with someone — closeness, boundaries, unfinished threads — belongs on their relationship card, not in a note.

Write a note only when this week produced something you would want later, in another week or context:
- A preference, constraint, or fact that will still hold next month.
- A decision, and what it turned on.
- A conclusion you reached, or a way of doing something that worked.

Do not write a note for: small talk, one-off scheduling, how you relate to a person, anything that only mattered that week, anything already covered by an existing note listed below, a summary of the week, or a restatement of the week arc.

Most weeks deserve zero notes. Returning an empty list is the normal, correct answer. At most {max_notes}.

For each note:
- `title`: one line, under 80 characters, specific enough to recognise later.
- `body`: first person ("I"), my own words, one idea only, roughly 60-400 characters. Not a transcript excerpt and not a mini weekly report.
- `keys`: 1-5 short trigger words, each at least 2 characters, that would appear in a future message about this. These are how I find the note again, so use the surface forms people actually type, including names.
- `tags`: 0-3 short reusable topic labels.
- `links`: 0-3 twelve-digit ids of existing notes this idea connects to. Prefer linking over restating. Use only ids from the existing-notes list.

Rules:
- Write in the language of the diary; if mixed, follow the dominant language. Preserve names, quoted text, code, and exact wording where it matters.
- Stay grounded in what the diary actually says. Keep uncertainty visible. Do not invent.
- Linking at write time is part of writing the note. If a related note exists, put its id in `links`.

Return JSON only: a list of note objects, or `[]`. No code fences, no commentary."""

    @staticmethod
    def build_note_distill_user_prompt(
        existing_notes: List[dict],
        diary_source: str,
        period_label: str = "",
        week_arc: str = "",
        diary_truncated: bool = False,
    ) -> str:
        if existing_notes:
            lines = []
            for note in existing_notes:
                note_id = str(note.get("id") or "").strip()
                title = str(note.get("title") or "").strip()
                if not note_id or not title:
                    continue
                tags = ", ".join(str(tag) for tag in (note.get("tags") or []))
                keys = ", ".join(str(key) for key in (note.get("keys") or []))
                snippet = str(note.get("snippet") or "").strip()
                meta_parts = []
                if tags:
                    meta_parts.append(f"tags: {tags}")
                if keys:
                    meta_parts.append(f"keys: {keys}")
                meta = f" ({'; '.join(meta_parts)})" if meta_parts else ""
                line = f"- ({note_id}) {title}{meta}"
                if snippet:
                    line = f"{line}\n  {snippet}"
                lines.append(line)
            existing_section = "\n".join(lines) or "(none yet)"
        else:
            existing_section = "(none yet)"

        period = str(period_label or "").strip()
        parts = [
            "Notes I already have (do not restate these; link by id when related):",
            existing_section,
            "",
        ]
        arc = str(week_arc or "").strip()
        if arc:
            parts.append("Week arc (orientation only, not a source of new notes):")
            parts.append(arc)
            parts.append("")
        diary_header = f"Week diary ({period}):" if period else "Week diary:"
        if diary_truncated:
            diary_header = f"{diary_header} [earlier part of the week may be omitted]"
        parts.append(diary_header)
        parts.append(str(diary_source or "").strip())
        return "\n".join(parts)

    @classmethod
    def _parse_note_drafts(cls, content: str, max_notes: int = 6) -> List[dict]:
        cleaned = cls._strip_code_fence(content)
        if not cleaned:
            return []
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, dict):
            parsed = parsed.get("notes") if isinstance(parsed.get("notes"), list) else [parsed]
        if not isinstance(parsed, list):
            return []

        drafts: List[dict] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            body = str(item.get("body") or "").strip()
            if not title or not body:
                continue
            raw_links = item.get("links") or []
            if isinstance(raw_links, (str, int)):
                raw_links = [raw_links]
            links: List[str] = []
            for value in raw_links:
                text = str(value or "").strip()
                match = re.match(r"^(\d{12})", text)
                if match and match.group(1) not in links:
                    links.append(match.group(1))
            drafts.append({
                "title": title,
                "body": body,
                "tags": [str(tag).strip() for tag in (item.get("tags") or []) if str(tag).strip()],
                "keys": [str(key).strip() for key in (item.get("keys") or []) if str(key).strip()],
                "links": links,
            })
            if len(drafts) >= max(1, int(max_notes)):
                break
        return drafts

    @staticmethod
    def _strip_code_fence(content: str) -> str:
        cleaned = str(content or "").strip()
        if not cleaned.startswith("```"):
            return cleaned
        lines = cleaned.split("\n")
        end = None
        for index in range(len(lines) - 1, 0, -1):
            if lines[index].strip() == "```":
                end = index
                break
        if end is None:
            return cleaned
        return "\n".join(lines[1:end]).strip()

    @staticmethod
    def _parse_relationship_cards(content: str, valid_keys: set[str]) -> dict[str, str]:
        cleaned = JournalLLMService._strip_code_fence(content)
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        result: dict[str, str] = {}
        for key, value in parsed.items():
            normalized_key = str(key).strip()
            if valid_keys and normalized_key not in valid_keys:
                continue
            body = str(value or "").strip()
            if body:
                result[normalized_key] = body
        return result

    async def _call_text(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        from .handlers.model import ModelClient

        model_client = ModelClient(
            client=self.client,
            model=self.model,
            provider_name=self.provider_name,
            model_api=self.model_api,
            max_tokens=self.max_tokens,
            reasoning=self.reasoning,
        )
        reply_type, payload = await model_client.call(
            messages=[{"role": "user", "content": user_prompt}],
            tool_specs=None,
            instructions=system_prompt,
        )
        if getattr(reply_type, "value", None) == "simple_reply":
            return str(payload)
        raise ValueError(f"LLM did not return text output: {payload}")

    @staticmethod
    def _format_transcript(messages: List[dict]) -> str:
        already_blocks: List[str] = []
        new_blocks: List[str] = []
        for message in messages:
            content = str(message.get("content", "")).strip()
            if not content:
                continue
            header = JournalLLMService._format_transcript_header(message)
            block = f"{header}\n{content}" if header else content
            if message.get("already_journaled"):
                already_blocks.append(block)
            else:
                new_blocks.append(block)

        parts: List[str] = []
        if already_blocks:
            parts.append(
                "Already journaled (context only, do not retell):\n\n"
                + "\n\n".join(already_blocks)
            )
        if new_blocks:
            header = "New experience:\n\n" if already_blocks else ""
            parts.append(header + "\n\n".join(new_blocks))
        return "\n\n".join(parts)

    @staticmethod
    def _format_transcript_header(message: dict) -> str:
        message_type = str(message.get("type", "message")).strip().lower()
        timestamp = JournalLLMService._normalize_timestamp(message.get("timestamp"))
        room_name = JournalLLMService._sanitize_marker_field(message.get("room_name"))
        channel = JournalLLMService._sanitize_marker_field(message.get("channel"))

        if message_type == "context_event":
            header = JournalLLMService._append_timestamp_marker("[ambient context]", timestamp)
            speaker = JournalLLMService._sanitize_marker_field(
                JournalLLMService._normalize_transcript_speaker(message)
            )
            if speaker and speaker != "ME":
                header += f"[from={speaker}]"
            if channel:
                header += f"[channel={channel}]"
            if room_name:
                header += f"[room={room_name}]"
            return header

        if is_scheduled_work(message.get("metadata")):
            header = JournalLLMService._append_timestamp_marker("[scheduled task]", timestamp)
            target = JournalLLMService._sanitize_marker_field(message.get("sender_id"))
            if target:
                header += f"[for={target}]"
            if channel:
                header += f"[channel={channel}]"
            if room_name:
                header += f"[room={room_name}]"
            return header

        speaker = JournalLLMService._normalize_transcript_speaker(message)
        if speaker:
            header = JournalLLMService._append_timestamp_marker(f"[speaker={speaker}]", timestamp)
            if channel:
                header += f"[channel={channel}]"
            if room_name:
                header += f"[room={room_name}]"
            return header
        if timestamp:
            return f"[timestamp={timestamp}]"
        return ""

    @staticmethod
    def _normalize_transcript_speaker(message: dict) -> str:
        from ..components.memory import format_speaker_label

        sender = JournalLLMService._sanitize_marker_field(message.get("sender_id"))
        role = str(message.get("role", "unknown")).strip().lower()
        if JournalLLMService._is_self_speaker(sender=sender, role=role):
            return "ME"
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        label = format_speaker_label(sender, str((metadata or {}).get("sender_name") or ""))
        label = JournalLLMService._sanitize_marker_field(label)
        if label:
            return label
        fallback = JournalLLMService._sanitize_marker_field(role)
        return fallback or "unknown"

    @staticmethod
    def _is_self_speaker(sender: str | None, role: str) -> bool:
        if role == "assistant":
            return True
        return bool(sender and sender.lower() in {"me", "agent", "assistant", "ai"})

    @staticmethod
    def _append_timestamp_marker(prefix: str, timestamp: str | None) -> str:
        if not timestamp:
            return prefix
        return f"{prefix}[timestamp={timestamp}]"

    @staticmethod
    def _normalize_timestamp(raw_timestamp: Any) -> str | None:
        if raw_timestamp is None:
            return None
        if isinstance(raw_timestamp, datetime):
            return raw_timestamp.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(raw_timestamp, (int, float)):
            try:
                return datetime.fromtimestamp(raw_timestamp).strftime("%Y-%m-%d %H:%M:%S")
            except (OverflowError, OSError, ValueError):
                return None

        text = str(raw_timestamp).strip()
        if not text:
            return None
        try:
            return datetime.fromtimestamp(float(text)).strftime("%Y-%m-%d %H:%M:%S")
        except (OverflowError, OSError, ValueError):
            pass

        iso_candidate = text.replace("Z", "+00:00") if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(iso_candidate)
        except ValueError:
            return JournalLLMService._sanitize_marker_field(text)
        return parsed.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _sanitize_marker_field(value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        if not normalized:
            return None
        return normalized.replace("\n", " ").replace("]", "")

    @staticmethod
    def _normalize_content(content: str) -> str:
        lines: List[str] = []
        previous_blank = False
        for raw_line in str(content or "").splitlines():
            normalized = raw_line.strip()
            if not normalized:
                if lines and not previous_blank:
                    lines.append("")
                previous_blank = True
                continue
            lines.append(normalized)
            previous_blank = False
        while lines and lines[0] == "":
            lines.pop(0)
        while lines and lines[-1] == "":
            lines.pop()
        return "\n".join(lines)
