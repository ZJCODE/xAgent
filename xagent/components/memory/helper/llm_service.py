"""Simplified LLM service for diary entry formatting and summary generation."""

import logging
from datetime import datetime
from typing import List

from openai import AsyncOpenAI

from ....schemas.memory import DiaryEntry, SummaryOutput


class JournalLLMService:
    """LLM service for formatting diary entries and generating periodic summaries."""

    def __init__(self, model: str = "gpt-5-mini"):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.openai_client = AsyncOpenAI()
        self.model = model

    async def format_diary_entry(
        self,
        messages: List[dict],
        journal_date: str,
    ) -> str:
        """Format conversation messages into a diary-style prose entry.

        Returns plain text suitable for appending to the daily markdown file.
        """
        if not messages:
            return ""

        transcript = self._format_transcript(messages)
        current_date = datetime.now().strftime("%Y-%m-%d")

        system_prompt = f"""You are writing a daily diary entry from a first-person observer perspective.

CURRENT DATE: {current_date}
TARGET JOURNAL DATE: {journal_date}

Writing requirements:
- Write in first person. Refer to the observer as "I".
- Any "agent", "assistant", or "AI" speaker in the transcript refers to me. Rewrite from my own point of view.
- Write it as my own diary after participating in those conversations.
- The writing perspective should feel like I am recalling interactions, with a natural and restrained tone.
- Do not replay the transcript line by line. Synthesize the important points.
- Keep the original language of the transcript. Do not translate.
- Preserve important details: distinctive wording, commitments, preferences, emotional tone.
- Different users must stay clearly separated. Never merge one user's content into another's.
- Aim for 100-300 characters when the source is brief, 200-500 when substantial.
- This is only a diary entry. Do not give advice, proposals, or recommendations.
- Do not end with offers to help or assistant-style closing language.

Return plain text as a natural diary-style entry."""

        user_prompt = f"""For {journal_date}, write a diary entry based on this conversation transcript:

{transcript}"""

        try:
            response = await self.openai_client.responses.parse(
                model=self.model,
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                text_format=DiaryEntry,
            )
            parsed = response.output_parsed or DiaryEntry()
            return self._normalize_content(parsed.content)
        except Exception as exc:
            self.logger.error("Error formatting diary entry: %s", exc)
            return self._fallback_entry(messages)

    async def generate_summary(
        self,
        source_content: str,
        period_type: str,
        period_label: str,
    ) -> str:
        """Generate a periodic summary (weekly/monthly/yearly) from source material.

        Args:
            source_content: The raw diary/summary text to summarize.
            period_type: One of ``weekly``, ``monthly``, ``yearly``.
            period_label: Human-readable label (e.g. "2026-03-16 to 2026-03-22").
        """
        if not source_content.strip():
            return ""

        system_prompt = f"""You are generating a {period_type} summary of diary entries from a first-person perspective.

PERIOD: {period_label}

Requirements:
- Write in first person ("I").
- Synthesize the key themes, events, decisions, and changes from the source material.
- Highlight notable interactions, preferences expressed, commitments made, and emotional shifts.
- Keep the original language. Do not translate.
- For weekly: focus on the main arc of the week, key people and what they were doing, important decisions.
- For monthly: focus on broader themes, recurring patterns, major milestones.
- For yearly: focus on the big picture — major phases, turning points, growth areas.
- This is a summary, not advice. Do not give recommendations or next steps.
- Keep it concise but complete. Aim for 300-800 characters for weekly, 500-1200 for monthly, 800-2000 for yearly."""

        user_prompt = f"""Generate a {period_type} summary for {period_label} based on this source material:

{source_content}"""

        try:
            response = await self.openai_client.responses.parse(
                model=self.model,
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                text_format=SummaryOutput,
            )
            parsed = response.output_parsed or SummaryOutput()
            return self._normalize_content(parsed.content)
        except Exception as exc:
            self.logger.error("Error generating %s summary: %s", period_type, exc)
            return ""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_transcript(messages: List[dict]) -> str:
        """Format messages into a simple transcript string."""
        lines: List[str] = []
        for msg in messages:
            role = msg.get("role", "unknown")
            sender = msg.get("sender_id", role)
            content = str(msg.get("content", "")).strip()
            if not content:
                continue
            lines.append(f"[{sender}]: {content}")
        return "\n".join(lines)

    @staticmethod
    def _normalize_content(content: str) -> str:
        """Collapse excessive blank lines and strip edges."""
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
        """Simple fallback when the LLM call fails."""
        parts: List[str] = []
        for msg in messages:
            content = str(msg.get("content", "")).strip()
            sender = msg.get("sender_id", msg.get("role", ""))
            if content:
                parts.append(f"{sender}: {content}")
        return "\n".join(parts)
