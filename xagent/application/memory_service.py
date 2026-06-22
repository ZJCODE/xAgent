"""LLM-backed formatting for diary memory."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, List, Optional

from openai import AsyncOpenAI

from ..config.providers import MODEL_API_OPENAI_CHAT_COMPLETIONS


class JournalFormatter:
    """Format conversation snippets and summaries for the diary memory store."""

    def __init__(
        self,
        client: Optional[Any] = None,
        model: str = "gpt-5.4-mini",
        model_api: str = MODEL_API_OPENAI_CHAT_COMPLETIONS,
        max_tokens: int = 4096,
    ) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)
        self.client = client or AsyncOpenAI()
        self.model = model
        self.model_api = model_api
        self.max_tokens = max_tokens

    async def format_diary_entry(
        self,
        messages: List[dict],
        journal_date: str,
    ) -> str:
        """Format conversation messages into diary prose for one day."""
        if not messages:
            return ""

        transcript = self._format_transcript(messages)
        system_prompt = self.build_diary_system_prompt(journal_date)
        user_prompt = self.build_diary_user_prompt(journal_date, transcript)

        try:
            content = await self._call_text(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            return self._normalize_content(content)
        except Exception as exception:
            self.logger.error("Error formatting diary entry: %s", exception)
            return self._fallback_entry(messages)

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
    def build_diary_system_prompt(journal_date: str) -> str:
        return """Write a concise diary entry from my first-person perspective.

Input markers:
- `[speaker=Name][timestamp=Time]`: Name said or wrote this.
- `[speaker=ME][timestamp=Time]`, `ME ...` in room context, or assistant/agent/AI roles: I said or did this.
- `[ambient context][timestamp=Time]`: something I noticed, overheard, or received.
- `[room context]` blocks identify a room with `room_name: ...`, `room_id: ...`, lines like `Name YYYY-MM-DD HH:mm: text`, and `[/room context]`.

Rules:
- Treat the transcript as my own experience stream, not a user-owned log or searchable database.
- Use "I"; keep the source language; synthesize the period's arc instead of replaying a transcript.
- Keep people, rooms, preferences, commitments, and experiences separate.
- First-person words in non-ME entries belong to that speaker, not to me.
- Ambient context is not a direct request unless it says it was addressed to me.
- Use timestamps only for ordering and attribution. Do not repeat markers, metadata, or timestamps.
- Preserve durable details and uncertainty. Aim for 100-300 characters for brief sources, 200-500 for substantial sources.

- Return only the diary entry text. No advice, JSON, code fences, or explanatory prose."""

    @staticmethod
    def build_diary_user_prompt(journal_date: str, transcript: str) -> str:
        return f"""For {journal_date}, write a diary entry from this transcript:

{transcript}"""

    @staticmethod
    def build_summary_system_prompt(period_type: str, period_label: str) -> str:
        return f"""Write a concise {period_type} summary of my diary entries in first person.

Rules:
- The source is my memory as an independent individual, not user-owned records.
- Use "I"; keep the source language; synthesize themes, decisions, commitments, preferences, emotional shifts, and durable changes.
- Preserve attribution. Keep people, rooms, plans, and experiences attached to the right source.
- Treat generic labels such as "User A" or "User B" as local unless continuity is explicit.
- Use `# YYYY-MM-DD` and `## HH:MM` only for chronology; do not repeat headings unless useful.
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

    async def _call_text(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        from ..infrastructure.llm import ModelClient

        model_client = ModelClient(
            client=self.client,
            model=self.model,
            model_api=self.model_api,
            max_tokens=self.max_tokens,
        )
        text_parts: list[str] = []
        async for event in model_client.model_turn_events(
            messages=[{"role": "user", "content": user_prompt}],
            tool_specs=None,
            instructions=system_prompt,
            stream=False,
        ):
            if event.type in {"text", "delta"} and event.delta:
                text_parts.append(event.delta)
            elif event.type == "error":
                raise ValueError(f"LLM did not return text output: {event.error}")
        return "".join(text_parts)

    @staticmethod
    def _format_transcript(messages: List[dict]) -> str:
        blocks: List[str] = []
        for message in messages:
            content = str(message.get("content", "")).strip()
            if not content:
                continue
            header = JournalFormatter._format_transcript_header(message)
            blocks.append(f"{header}\n{content}" if header else content)
        return "\n\n".join(blocks)

    @staticmethod
    def _format_transcript_header(message: dict) -> str:
        message_type = str(message.get("type", "message")).strip().lower()
        timestamp = JournalFormatter._normalize_timestamp(message.get("timestamp"))
        if message_type == "context_event":
            return JournalFormatter._append_timestamp_marker("[ambient context]", timestamp)

        speaker = JournalFormatter._normalize_transcript_speaker(message)
        if speaker:
            return JournalFormatter._append_timestamp_marker(f"[speaker={speaker}]", timestamp)
        if timestamp:
            return f"[timestamp={timestamp}]"
        return ""

    @staticmethod
    def _normalize_transcript_speaker(message: dict) -> str:
        sender = JournalFormatter._sanitize_marker_field(message.get("sender_id"))
        role = str(message.get("role", "unknown")).strip().lower()
        if JournalFormatter._is_self_speaker(sender=sender, role=role):
            return "ME"
        if sender:
            return sender
        fallback = JournalFormatter._sanitize_marker_field(role)
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
            return JournalFormatter._sanitize_marker_field(text)
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
            line = raw_line.rstrip()
            if not line:
                if not previous_blank:
                    lines.append("")
                previous_blank = True
                continue
            lines.append(line)
            previous_blank = False
        return "\n".join(lines).strip()

    @staticmethod
    def _fallback_entry(messages: List[dict]) -> str:
        return JournalFormatter._format_transcript(messages)
