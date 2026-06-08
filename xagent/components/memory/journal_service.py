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

Source meaning:
- The transcript is my experience stream: direct conversations, my replies, observations, overheard speech, notifications, reminders, and other received context.
- Speakers named "ME", "agent", "assistant", or "AI" are me; rewrite them as what I did, said, or noticed.
- First-person words ("I", "me", "my", "我") inside another speaker's line belong to that speaker, not to me; never adopt another person's "I" as my own.

Writing rules:
- Use "I" and a natural, restrained diary tone.
- Synthesize important points instead of replaying the transcript line by line.
- Keep the source language and do not translate.
- Preserve distinctive wording, commitments, preferences, emotional tone, and other durable details.
- Aim for 100-300 characters for brief sources, 200-500 for substantial sources.

Attribution rules:
- Keep different people separate; never merge one person's facts, preferences, plans, or experiences into another's.
- Attribute important facts to the speaker or source that said, experienced, or provided them.
- For ambient context, use forms such as "I noticed...", "I overheard...", or "A notification arrived...".
- Never turn overheard speech or ambient observations into a direct request unless the source says it was addressed to me.
- If attribution is uncertain, keep the uncertainty visible.

Output rules:
- This is only a diary entry; do not give advice, proposals, recommendations, next steps, or assistant-style closings.
- Return only the diary entry text. Do not wrap it in JSON, markdown code fences, or explanatory prose."""

    @staticmethod
    def build_diary_user_prompt(journal_date: str, transcript: str) -> str:
        return f"""For {journal_date}, write a diary entry based on this conversation transcript:

{transcript}"""

    @staticmethod
    def build_summary_system_prompt(period_type: str, period_label: str) -> str:
        return f"""Write a concise {period_type} summary of diary entries from my first-person perspective.

PERIOD: {period_label}

Summary rules:
- Use "I"; keep the source language and do not translate.
- Synthesize key themes, events, decisions, commitments, preferences, emotional shifts, and durable changes.
- Preserve speaker attribution. Do not flatten multiple people into one undifferentiated narrative.
- Keep each person's preferences, plans, commitments, and experiences attached to that person.
- Treat generic labels such as "User A", "User B", "用户A", or "用户B" as local to one source entry unless continuity is explicit.
- If attribution is uncertain, keep the uncertainty visible.

Period focus:
- Weekly: main arc, key people, what they were doing, and important decisions.
- Monthly: broader themes, recurring patterns, and major milestones.
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
            output_type=None,
        )
        if getattr(reply_type, "value", None) == "simple_reply":
            return str(payload)
        raise ValueError(f"LLM did not return text output: {payload}")

    @staticmethod
    def _format_transcript(messages: List[dict]) -> str:
        lines: List[str] = []
        for message in messages:
            message_type = str(message.get("type", "message"))
            role = message.get("role", "unknown")
            sender = message.get("sender_id", role)
            content = str(message.get("content", "")).strip()
            if content:
                if message_type == "context_event":
                    lines.append(f"[ambient context]: {content}")
                else:
                    lines.append(f"[{sender}]: {content}")
        return "\n".join(lines)

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
        parts: List[str] = []
        for message in messages:
            content = str(message.get("content", "")).strip()
            sender = message.get("sender_id", message.get("role", ""))
            if content:
                parts.append(f"{sender}: {content}")
        return "\n".join(parts)
