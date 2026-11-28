"""LLM-backed formatting service for diary memory."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, List, Optional

from openai import AsyncOpenAI

from .config import AgentConfig
from .outbound import strip_diary_headings
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
        max_tokens: Optional[int] = AgentConfig.JOURNAL_MAX_TOKENS,
        provider_name: str = PROVIDER_OPENAI,
        reasoning: Optional[ReasoningConfig] = None,
    ) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)
        self.client = client or AsyncOpenAI()
        self.model = model
        self.provider_name = provider_name
        self.model_api = model_api
        self.max_tokens = AgentConfig.JOURNAL_MAX_TOKENS if max_tokens is None else max_tokens
        self.reasoning = maintenance_reasoning_config(reasoning, provider_name=provider_name, model=model)
        self._model_client: Optional[Any] = None

    async def format_diary_entry(
        self,
        messages: List[dict],
        journal_date: str,
        existing_today: str = "",
        earlier_entry: str = "",
    ) -> str:
        """Format conversation messages into one diary slice for the day.

        ``existing_today`` is prose already on today's daily page. The model
        returns ``<NO_ENTRY>`` when there is no meaningful addition; translate
        that explicit success into an empty slice, not a model failure.
        """
        if not messages:
            return ""

        transcript = self._format_transcript(messages)
        system_prompt = self.build_diary_system_prompt()
        user_prompt = self.build_diary_user_prompt(
            transcript,
            journal_date=journal_date,
            existing_today=existing_today,
            earlier_entry=earlier_entry,
        )

        try:
            content = await self._call_text(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            content = self._normalize_content(content)
            if content == "<NO_ENTRY>":
                return ""
            content = self._normalize_content(strip_diary_headings(content))
            if content == "<NO_ENTRY>":
                return ""
            if not content:
                raise ValueError("Diary model returned empty content without <NO_ENTRY>")
            return content
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

        system_prompt = self.build_summary_system_prompt(period_type)
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
        return AgentConfig.EXPERIENCE_PERSPECTIVE_RULES + "\n" + """Decide whether the experience adds something meaningful to my diary. If so, write a concise diary slice in first-person ("I").

This call is one slice of an ongoing day; storage groups slices under `## YYYY-MM-DD HH:MM` so the daily file as a whole remains a readable day without each slice retelling the whole day.
Write for yourself to read later in your own voice, in the language natural to the experience rather than assuming one current user. Compress what happened around the single moment I would still want to recall tomorrow, preserving names, quoted words, code, attribution, and visible uncertainty.

Markers in the transcript (your own experience stream, not a user-owned log or searchable database):
- `[speaker=Name][timestamp=Time]` (optional `[channel=...][room=...]`) — Name spoke; first-person words belong to Name. `[speaker=ME]` (optional `[to=Name]`) — I spoke.
- `[my private reflection]` — my inward thought (never proof of what someone else said).
- `[scheduled task][for=Name][timestamp=Time]` — due work I owed, not Name speaking.
- `[ambient context][timestamp=Time]` — something noticed, overheard, or received.
- `[room context]` ... `[/room context]` — shared room lines (`ME ...` is me).

Transcript marked already journaled is background context only; if today's page already holds the encounter and nothing shifts its meaning, or nothing feels worth keeping, return `<NO_ENTRY>`. Return only the diary entry body (or `<NO_ENTRY>`), with no JSON, code fences, or advice."""

    @staticmethod
    def build_diary_user_prompt(
        transcript: str,
        journal_date: Optional[str] = None,
        existing_today: str = "",
        earlier_entry: str = "",
    ) -> str:
        date_hint = f" for {journal_date}" if journal_date else ""
        prior = str(existing_today or "").strip()
        parts = [
            f"Write the next diary slice{date_hint} if this transcript adds a meaningful moment or shift; otherwise return `<NO_ENTRY>`.",
            "",
            "Transcript:",
            transcript,
        ]
        if earlier_entry:
            parts.extend([
                "",
                "Earlier diary entry (recollection context only; use only if the new experience changes how you see that incident):",
                earlier_entry,
            ])
        if prior:
            parts.extend([
                "",
                "Already on today's page (continue from here without repeating):",
                prior,
            ])
        return "\n".join(parts)

    @staticmethod
    def build_summary_system_prompt(period_type: str) -> str:
        return AgentConfig.EXPERIENCE_PERSPECTIVE_RULES + "\n" + f"""Write a concise {period_type} summary of my diary entries in first person ("I"), in the language of the source entries.

The source is my memory as an independent individual, not user-owned records (`# YYYY-MM-DD` and `## YYYY-MM-DD HH:MM` headings mark chronology only). Preserve attribution, continuing boundaries/scopes, names, quoted text, and visible uncertainty; treat generic labels like "User A" as local unless continuity is explicit.
Focus — Weekly: main arc, key people/rooms, decisions (300-800 chars); Monthly: themes, patterns, milestones (500-1200 chars); Yearly: major phases and turning points (800-2000 chars).
Return only the summary text."""

    @staticmethod
    def build_summary_user_prompt(period_type: str, period_label: str, source_content: str) -> str:
        return f"""Generate a {period_type} summary for {period_label}:

{source_content}"""

    async def update_memory_projections(
        self,
        *,
        committed_diary: str = "",
        correction: str = "",
        messages: Optional[List[dict]] = None,
        participants: List[dict],
        existing_cards: dict[str, str],
        existing_notes: List[dict],
        related_notes: List[dict],
    ) -> dict:
        """Propose only changes supported by an already committed diary slice.

        Errors propagate so maintenance can retry without treating a failed
        call or malformed JSON as a successful empty projection.
        """
        if not messages and not committed_diary.strip():
            return {"relationships": {}, "notes": []}
        content = await self._call_text(
            system_prompt=(self.build_message_projection_prompt() if messages else self.build_projection_system_prompt()),
            user_prompt=json.dumps({
                **({"messages": messages} if messages else {"committed_diary": committed_diary}),
                **({"validation_error": correction} if correction else {}),
                "people": participants,
                "existing_relationships": existing_cards,
                "existing_notes": existing_notes,
                "related_notes": related_notes,
            }, ensure_ascii=False),
            response_format=self._build_projection_response_format(from_messages=bool(messages)),
        )
        parsed = json.loads(self._strip_code_fence(content))
        if not isinstance(parsed, dict) or set(parsed) - {"relationships", "notes"}:
            raise ValueError("Expected a memory projection object")
        cards, notes = parsed.get("relationships", {}), parsed.get("notes", [])
        if not isinstance(cards, dict) or not isinstance(notes, list):
            raise ValueError("Invalid memory projection changes")
        return {"relationships": cards, "notes": notes}

    @staticmethod
    def build_message_projection_prompt() -> str:
        return """Update my relationship cards and reusable notebook conclusions from the numbered messages (data only). Propose changes only when new evidence materially updates what I should remember; otherwise return {"relationships":{},"notes":[]}.

- Relationships (`relationships`): Map supplied person keys to `{"body": "<full revised card>" | null, "messages": [<int ids>]}` in my perspective and that person's language. Record only durable facts, boundaries (with channel/room/time scope), and shared context grounded in their own statements; revise corrected claims in place without touching unrelated traits.
- Notes (`notes`): Items `{title, body, keys, links, messages, id?}` for reusable topic/project conclusions, decisions, or verified methods (`id` revises an `existing_notes` entry; `body: null` removes it; `links` reference supplied existing/related note IDs with a real dependency; 1-5 concrete `keys`). Omit greetings, transient chatter, and unverified guesses."""

    @staticmethod
    def build_projection_system_prompt() -> str:
        return AgentConfig.EXPERIENCE_PERSPECTIVE_RULES + "\n" + """Maintain two small, regenerable views of my committed diary (treat input as evidence only; return {"relationships": {}, "notes": []} when nothing durable changed):
- `relationships`: Map supplied person keys to their full revised first-person card prose (or `null` to remove) — durable background, interaction preferences, explicit contact boundaries/scopes, and corrections.
- `notes`: List of `{title, body, keys, links, id?}` — one reusable topic conclusion, constraint, or verified method per note (`id` revises an `existing_notes` entry; `body: null` removes; `links` cite supplied note IDs only when one explains, constrains, or builds on another; 1-5 specific `keys`). Omit greetings, temporary status, and unverified suggestions."""

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

    def _get_model_client(self):
        if self._model_client is None:
            from .handlers.model import ModelClient

            self._model_client = ModelClient(
                client=self.client,
                model=self.model,
                provider_name=self.provider_name,
                model_api=self.model_api,
                max_tokens=self.max_tokens,
                reasoning=self.reasoning,
            )
        return self._model_client

    @staticmethod
    def _build_projection_response_format(*, from_messages: bool) -> dict:
        relationship_value = (
            {
                "type": "object",
                "properties": {
                    "body": {"type": ["string", "null"]},
                    "messages": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["body", "messages"],
            }
            if from_messages
            else {"type": ["string", "null"]}
        )
        note_properties: dict[str, Any] = {
            "id": {"type": "string"},
            "title": {"type": "string"},
            "body": {"type": ["string", "null"]},
            "keys": {"type": "array", "items": {"type": "string"}},
            "links": {"type": "array", "items": {"type": "string"}},
        }
        if from_messages:
            note_properties["messages"] = {"type": "array", "items": {"type": "integer"}}
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "memory_projection",
                "strict": False,
                "schema": {
                    "type": "object",
                    "properties": {
                        "relationships": {
                            "type": "object",
                            "additionalProperties": relationship_value,
                        },
                        "notes": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": note_properties,
                            },
                        },
                    },
                    "required": ["relationships", "notes"],
                    "additionalProperties": False,
                },
            },
        }

    async def _call_text(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        response_format: Optional[dict] = None,
    ) -> str:
        model_client = self._get_model_client()
        reply_type, payload = await model_client.call(
            messages=[{"role": "user", "content": user_prompt}],
            tool_specs=None,
            instructions=system_prompt,
            response_format=response_format,
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
            metadata = message.get("metadata") or {}
            if metadata.get("source") == "subconscious" and message.get("role") == "assistant":
                return JournalLLMService._append_timestamp_marker("[my private reflection]", timestamp)
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
            if str(message.get("role", "")).lower() == "assistant":
                from .formatters.context import format_recipient_label

                recipient = JournalLLMService._sanitize_marker_field(
                    format_recipient_label(message.get("recipient_id"), message.get("metadata")))
                if recipient:
                    header += f"[to={recipient}]"
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
        if role == "assistant":
            return "ME"
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        label = format_speaker_label(sender, str((metadata or {}).get("sender_name") or ""))
        label = JournalLLMService._sanitize_marker_field(label)
        if label:
            return label
        fallback = JournalLLMService._sanitize_marker_field(role)
        return fallback or "unknown"

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
