"""LLM-backed formatting service for diary memory."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, List, Optional

from openai import AsyncOpenAI
from pydantic import BaseModel

from ...schemas.memory import DiaryEntry, PeopleProfileUpdates, SummaryOutput


class JournalLLMService:
    """Format conversation snippets and summaries for the diary memory store."""

    def __init__(
        self,
        client: Optional[Any] = None,
        model: str = "gpt-5.4-mini",
        backend: str = "openai",
        max_tokens: int = 4096,
    ) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)
        self.client = client or AsyncOpenAI()
        self.model = model
        self.backend = backend
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
            parsed = await self._call_structured(
                output_type=DiaryEntry,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            return self._normalize_content(parsed.content)
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
            parsed = await self._call_structured(
                output_type=SummaryOutput,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            return self._normalize_content(parsed.content)
        except Exception as exception:
            self.logger.error("Error generating %s summary: %s", period_type, exception)
            return ""

    async def extract_people_profile_updates(
        self,
        messages: List[dict],
        diary_entry: str,
        journal_date: str,
    ) -> PeopleProfileUpdates:
        """Extract quote-backed stable people facts from a diary source batch."""
        if not messages:
            return PeopleProfileUpdates()

        transcript = self._format_transcript(messages)
        if not transcript.strip():
            return PeopleProfileUpdates()

        system_prompt = self.build_people_profile_system_prompt(journal_date)
        user_prompt = self.build_people_profile_user_prompt(
            journal_date=journal_date,
            transcript=transcript,
            diary_entry=diary_entry,
        )

        try:
            parsed = await self._call_structured(
                output_type=PeopleProfileUpdates,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            return parsed
        except Exception as exception:
            self.logger.error("Error extracting people profile updates: %s", exception)
            return PeopleProfileUpdates()

    @staticmethod
    def build_diary_system_prompt(journal_date: str, current_date: str | None = None) -> str:
        current_date = current_date or datetime.now().strftime("%Y-%m-%d")
        return f"""You are writing a daily diary entry from a first-person observer perspective.

CURRENT DATE: {current_date}
TARGET JOURNAL DATE: {journal_date}

Writing requirements:
- Write in first person. Refer to the observer as "I".
- The source material is my experience stream: direct conversations, my replies, observations, overheard speech, notifications, reminders, and other context I received.
- Any "agent", "assistant", or "AI" speaker in the transcript refers to me. Rewrite from my own point of view.
- Write it as my own diary after participating in those conversations and experiencing those observations.
- The writing perspective should feel like I am recalling interactions, with a natural and restrained tone.
- Do not replay the transcript line by line. Synthesize the important points.
- Keep the original language of the transcript. Do not translate.
- Preserve important details: distinctive wording, commitments, preferences, emotional tone.
- Different users must stay clearly separated. Never merge one user's content into another's.
- Every important fact must remain attributed to the speaker who said or experienced it.
- Prefer explicit attribution phrases such as "With abc, ...", "jun mentioned ...", or "T preferred ..." when multiple speakers appear.
- For observations, use attribution such as "I noticed...", "I overheard Bob say...", or "A notification arrived...".
- Never rewrite overheard speech or ambient observations as a direct request from the current user unless the source says it was addressed to me.
- Never imply that different speakers shared the same preference, plan, event, or history unless the source explicitly says so.
- If attribution is uncertain, keep that uncertainty instead of collapsing multiple speakers into one narrative.
- Aim for 100-300 characters when the source is brief, 200-500 when substantial.
- This is only a diary entry. Do not give advice, proposals, or recommendations.
- Do not end with offers to help or assistant-style closing language.

Return JSON only, shaped as {{"content": "natural diary-style entry"}}."""

    @staticmethod
    def build_diary_user_prompt(journal_date: str, transcript: str) -> str:
        return f"""For {journal_date}, write a diary entry based on this conversation transcript:

{transcript}"""

    @staticmethod
    def build_summary_system_prompt(period_type: str, period_label: str) -> str:
        return f"""You are generating a {period_type} summary of diary entries from a first-person perspective.

PERIOD: {period_label}

Requirements:
- Write in first person ("I").
- Synthesize the key themes, events, decisions, and changes from the source material.
- Highlight notable interactions, preferences expressed, commitments made, and emotional shifts.
- Keep the original language. Do not translate.
- Preserve speaker attribution throughout the summary. Do not flatten multiple people into one profile.
- When several speakers appear, summarize them separately or in clearly attributed clauses.
- Preferences, plans, commitments, and experiences must stay attached to the speaker who originally expressed or experienced them.
- Generic labels such as "User A", "User B", "用户A", or "用户B" are local aliases inside a single source entry; do not merge them across different entries unless continuity is explicit.
- If the source material leaves attribution uncertain, keep that uncertainty visible in the summary.
- For weekly: focus on the main arc of the week, key people and what they were doing, important decisions.
- For monthly: focus on broader themes, recurring patterns, major milestones.
- For yearly: focus on the big picture - major phases, turning points, growth areas.
- This is a summary, not advice. Do not give recommendations or next steps.
- Keep it concise but complete. Aim for 300-800 characters for weekly, 500-1200 for monthly, 800-2000 for yearly.
- Return JSON only, shaped as {{"content": "period summary"}}."""

    @staticmethod
    def build_summary_user_prompt(period_type: str, period_label: str, source_content: str) -> str:
        return f"""Generate a {period_type} summary for {period_label} based on this source material:

{source_content}"""

    @staticmethod
    def build_people_profile_system_prompt(journal_date: str) -> str:
        return f"""You extract durable people profile facts from an experience transcript.

TARGET DATE: {journal_date}

Requirements:
- Return only stable, reusable facts about a person: preferences, roles, relationships, commitments, ongoing projects, long-term constraints, or recurring interaction patterns.
- Each update must be tied to exactly one person.
- person_key must be the exact speaker label from the transcript, such as the text inside [Alice].
- Do not create updates for [agent], [assistant], [ambient context], unknown speakers, or uncertain attribution.
- Do not infer personality labels from a single moment. Prefer concrete facts the person stated or clearly demonstrated.
- evidence is required. It must be a short direct quote or exact source phrase from the transcript.
- If no stable quote-backed people facts are present, return {{"updates": []}}.
- Keep the original language of the source. Do not translate.

Return JSON only, shaped as {{"updates": [{{"person_key": "speaker", "display_name": "name", "fact": "fact", "evidence": "quote", "source": "source note"}}]}}."""

    @staticmethod
    def build_people_profile_user_prompt(journal_date: str, transcript: str, diary_entry: str) -> str:
        return f"""For {journal_date}, extract people profile updates from this transcript.

Transcript:
{transcript}

Diary entry already written:
{diary_entry}"""

    async def _call_structured(
        self,
        output_type: type[BaseModel],
        system_prompt: str,
        user_prompt: str,
    ) -> BaseModel:
        from ...core.handlers.model import ModelClient

        model_client = ModelClient(
            client=self.client,
            model=self.model,
            backend=self.backend,
            max_tokens=self.max_tokens,
        )
        reply_type, payload = await model_client.call(
            messages=[{"role": "user", "content": user_prompt}],
            tool_specs=None,
            instructions=system_prompt,
            output_type=output_type,
        )
        if getattr(reply_type, "value", None) == "structured_reply":
            return payload
        raise ValueError(f"LLM did not return structured output: {payload}")

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
