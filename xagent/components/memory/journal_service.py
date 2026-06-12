"""LLM-backed formatting service for diary memory."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, List, Optional

from openai import AsyncOpenAI


DEFAULT_OPENAI_CHAT_MODEL_API = "openai_chat_completions"


class JournalLLMService:
    """Format conversation snippets and summaries for the diary memory store."""

    def __init__(
        self,
        client: Optional[Any] = None,
        model: str = "gpt-5.4-mini",
        model_api: str = DEFAULT_OPENAI_CHAT_MODEL_API,
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
    def build_diary_system_prompt(journal_date: str, current_date: str | None = None) -> str:
        current_date = current_date or datetime.now().strftime("%Y-%m-%d")
        return f"""Write a concise daily diary entry from my first-person perspective.

CURRENT DATE: {current_date}
TARGET JOURNAL DATE: {journal_date}

Structured history format:
- `[speaker=Name][timestamp=Time]` followed by text: Name said or wrote that text at that time.
- `[speaker=ME][timestamp=Time]` followed by text: I said or wrote that text at that time.
- `[ambient context][timestamp=Time]` followed by text: situational context I noticed, overheard, or received at that time.
- `[room context]` blocks start with `room_name: ...` and `room_id: ...`, contain lines like `Name YYYY-MM-DD HH:mm: text`, and end with `[/room context]`. These are group or multi-party conversations I participated in. Inside a room context block, `ME YYYY-MM-DD HH:mm: text` means my own reply in that room.
- First-person words inside any entry belong to the entry's speaker, not to me.
- Use timestamps only to understand ordering and attribution. Never repeat transcript markers, timestamps, or metadata in the diary entry.

Source meaning:
- The transcript is my experience stream: one-to-one conversations, group chats, my replies, observations, overheard speech, notifications, reminders, and other received context.
- I may appear as `[speaker=ME]` in direct conversations, as `ME` inside `[room context]` blocks, or under roles like "agent", "assistant", or "AI". In all cases this is me; rewrite as what I did, said, or noticed.

Writing rules:
- Use "I" and a natural, restrained diary tone.
- Synthesize important points instead of replaying the transcript line by line.
- The transcript covers the newly observed period since the previous diary write or journal checkpoint.
- Focus on the arc of this period rather than writing a line-by-line log.
- Keep the source language and do not translate.
- Preserve distinctive wording, commitments, preferences, emotional tone, and other durable details.
- Aim for 100-300 characters for brief sources, 200-500 for substantial sources.

Attribution rules:
- Keep different people separate; never merge one person's facts, preferences, plans, or experiences into another's.
- Attribute important facts to the speaker or source that said, experienced, or provided them.
- When writing about a group conversation, name the room as scene context and keep each participant's words and actions attributed to the correct person.
- For ambient context, use forms such as "I noticed...", "I overheard...", or "A notification arrived...".
- Never turn overheard speech or ambient observations into a direct request unless the source says it was addressed to me.
- If attribution is uncertain, keep the uncertainty visible.

Output rules:
- This is only a diary entry; do not give advice, proposals, recommendations, next steps, or assistant-style closings.
- Return only the diary entry text. Do not wrap it in JSON, markdown code fences, or explanatory prose."""

    @staticmethod
    def build_diary_user_prompt(journal_date: str, transcript: str) -> str:
        return f"""For {journal_date}, write a diary entry based on this structured conversation transcript:

{transcript}"""

    @staticmethod
    def build_summary_system_prompt(period_type: str, period_label: str) -> str:
        return f"""Write a concise {period_type} summary of diary entries from my first-person perspective.

PERIOD: {period_label}

Source material format:
- The source consists of my own diary entries written in first person across the {period_type} period.
- Named people mentioned in entries are distinct individuals. Their words, preferences, and experiences belong to them, not to me.
- Room names in entries mark group conversations I participated in. Each room has its own set of participants.
- `# YYYY-MM-DD` and `## HH:MM` headings mark date and time boundaries; use them for chronology, do not repeat them in the summary.

Summary rules:
- Use "I"; keep the source language and do not translate.
- Synthesize key themes, events, decisions, commitments, preferences, emotional shifts, and durable changes.
- Preserve speaker attribution. Do not flatten multiple people into one undifferentiated narrative.
- Keep each person's preferences, plans, commitments, and experiences attached to that person.
- When source entries reference group conversations (room context), keep the room's participants distinct and note the room as the scene.
- Treat generic labels such as "User A" or "User B" as local to one source entry unless continuity is explicit.
- If attribution is uncertain, keep the uncertainty visible.

Period focus:
- Weekly: main arc, key people and rooms, what people were doing, important decisions.
- Monthly: broader themes, recurring patterns across rooms and conversations, major milestones.
- Yearly: major phases, turning points, and growth areas.

Output rules:
- This is a summary, not advice; do not give recommendations or next steps.
- Aim for 300-800 characters for weekly, 500-1200 for monthly, 800-2000 for yearly.
- Return only the summary text. Do not wrap it in JSON, markdown code fences, or explanatory prose."""

    @staticmethod
    def build_summary_user_prompt(period_type: str, period_label: str, source_content: str) -> str:
        return f"""Generate a {period_type} summary for {period_label} based on this source material:

{source_content}"""

    async def _call_text(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        from ...core.handlers.model import ModelClient

        model_client = ModelClient(
            client=self.client,
            model=self.model,
            model_api=self.model_api,
            max_tokens=self.max_tokens,
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
        blocks: List[str] = []
        for message in messages:
            content = str(message.get("content", "")).strip()
            if not content:
                continue
            header = JournalLLMService._format_transcript_header(message)
            blocks.append(f"{header}\n{content}" if header else content)
        return "\n\n".join(blocks)

    @staticmethod
    def _format_transcript_header(message: dict) -> str:
        message_type = str(message.get("type", "message")).strip().lower()
        timestamp = JournalLLMService._normalize_timestamp(message.get("timestamp"))
        if message_type == "context_event":
            return JournalLLMService._append_timestamp_marker("[ambient context]", timestamp)

        speaker = JournalLLMService._normalize_transcript_speaker(message)
        if speaker:
            return JournalLLMService._append_timestamp_marker(f"[speaker={speaker}]", timestamp)
        if timestamp:
            return f"[timestamp={timestamp}]"
        return ""

    @staticmethod
    def _normalize_transcript_speaker(message: dict) -> str:
        sender = JournalLLMService._sanitize_marker_field(message.get("sender_id"))
        role = str(message.get("role", "unknown")).strip().lower()
        if JournalLLMService._is_self_speaker(sender=sender, role=role):
            return "ME"
        if sender:
            return sender
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

    @staticmethod
    def _fallback_entry(messages: List[dict]) -> str:
        return JournalLLMService._format_transcript(messages)
