import logging
from datetime import datetime
import re
from typing import List

from openai import AsyncOpenAI

from ....schemas.memory import DailyJournalRewrite, JournalKeywordExtraction


class JournalLLMService:
    """LLM service for rewriting daily journals and extracting retrieval keywords."""

    def __init__(self, model: str = "gpt-5-mini"):
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        self.openai_client = AsyncOpenAI()
        self.model = model

    async def rewrite_daily_journal(
        self,
        existing_journal: str,
        new_transcript: str,
        journal_date: str,
    ) -> str:
        """Rewrite the full journal text for one date from prior journal + new transcript."""
        current_date = datetime.now().strftime("%Y-%m-%d")
        self.logger.debug(
            "LLM journal rewrite request: journal_date=%s existing_len=%d transcript_len=%d model=%s",
            journal_date,
            len(existing_journal or ""),
            len(new_transcript or ""),
            self.model,
        )

        system_prompt = self._build_rewrite_system_prompt(
            current_date=current_date,
            journal_date=journal_date,
        )
        user_prompt = self._build_rewrite_user_prompt(
            existing_journal=existing_journal,
            new_transcript=new_transcript,
            journal_date=journal_date,
        )

        try:
            response = await self.openai_client.responses.parse(
                model=self.model,
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                text_format=DailyJournalRewrite,
            )
            parsed = response.output_parsed or DailyJournalRewrite()
            normalized = self._normalize_journal_content(parsed.content)
            self.logger.debug(
                "LLM journal rewrite success: journal_date=%s output_len=%d",
                journal_date,
                len(normalized),
            )
            return normalized
        except Exception as exc:
            self.logger.error("Error rewriting daily journal: %s", exc)
            fallback = self._normalize_journal_content(existing_journal)
            self.logger.debug(
                "LLM journal rewrite fallback to existing journal: journal_date=%s fallback_len=%d",
                journal_date,
                len(fallback),
            )
            return fallback

    def _build_rewrite_system_prompt(
        self,
        *,
        current_date: str,
        journal_date: str,
    ) -> str:
        return f"""You are writing a daily journal from a first-person observer perspective.

CURRENT DATE: {current_date}
TARGET JOURNAL DATE: {journal_date}

Writing requirements:
- Write in first person. Refer to the observer as "I" rather than “Assistant”, “AI”, “the assistant”, or other third-person labels.
- The journal is about conversations that happened when I was interacting with other people. Any "agent", "assistant", or "AI" speaker in the transcript refers to me and must be rewritten from my own first-person point of view.
- Do not describe the day as if I were merely watching a transcript from the outside. Write it as my own diary after participating in those conversations.
- The writing perspective should feel like I am recalling the day after interacting with people, with a natural and restrained tone.
- The journal should read like a human-like observation diary, not a system log, audit trail, or bullet-point report.
- Do not replay the transcript line by line. Synthesize the day's important movement, emphasis, and changes.
- Keep the original language of the transcript whenever possible. Do not translate unless the source already mixes languages.
- Preserve important details such as distinctive wording, unusual phrases, commitments, preferences, emotional tone, or clear changes in direction.
- Short quotes are allowed only when a phrase is especially revealing or memorable. Do not over-quote or paste long stretches of the transcript.
- Different users must stay clearly separated. Never merge one user's preferences, plans, or attitudes into another user's section.
- The transcript may already use normalized speaker labels such as "User A" or "User B". Treat those labels as canonical and preserve them exactly.
- If the existing journal already uses those normalized user labels, keep them stable in the rewrite. Do not reshuffle them.
- Aim for medium length, usually around 200-500 characters in the journal's primary language when the source material is substantial. If the day is sparse, stay brief but still complete the structure.
- Output one complete journal entry for the target date, not a delta. Merge the existing journal with the new transcript and remove obvious duplication.
- In the final section, the summary or feeling should also stay in first person, using the same observer pronoun, but keep it restrained.
- Let the journal naturally touch on the main movement of the day, who appeared and what each person was doing, notable preferences or expressions or changes, and a restrained closing impression from the observer.
- This is only a diary entry. Do not give advice, proposals, next steps, reminders, recommendations, or direct responses to any user.
- Do not end with offers to help, follow-up questions, or assistant-style closing language. End like a person finishing a private diary note.

Return plain text as a natural diary-style entry. Headings are optional. Smooth prose is preferred over rigid formatting.
"""

    def _build_rewrite_user_prompt(
        self,
        *,
        existing_journal: str,
        new_transcript: str,
        journal_date: str,
    ) -> str:
        return f"""For {journal_date}, rewrite the full daily journal entry.

Interpretation notes:
- The existing journal is today's earlier observation draft.
- The new transcript is today's newly arrived interaction fragment.
- If the new transcript does not materially change the understanding of the day, preserve the useful parts of the existing journal and only make necessary refinements.
- Speaker labels already shown in the transcript are the names you should use in the rewritten journal.
- Keep the whole diary in a first-person perspective, using "I" when referring to the narrator's summary, attention, or feeling.
- Treat any "agent", "assistant", or "AI" role inside the transcript as me. Rewrite those parts from my own point of view instead of referring to that speaker as someone separate.
- Do not let the narrator call itself “Assistant”, “AI assistant”, or similar role labels.
- Keep the text observational. Do not turn it into advice, a plan, a suggestion, a recommendation, or a reply addressed to someone else.

Existing journal draft:
{existing_journal or "(empty)"}

New transcript chunk:
{new_transcript}
"""

    async def extract_query_keywords(
        self,
        query: str,
        max_keywords: int = 5,
    ) -> List[str]:
        """Extract a compact keyword set for journal retrieval."""
        query = str(query or "").strip()
        if not query:
            self.logger.debug("Skipping keyword extraction: empty query")
            return []
        self.logger.debug(
            "LLM keyword extraction request: query=%r max_keywords=%d model=%s",
            query,
            max_keywords,
            self.model,
        )

        system_prompt = """Extract 3 to 5 retrieval keywords or short phrases from the user's search query.

Rules:
- Keep the original language.
- Prefer concrete entities, dates, topics, projects, locations, and commitments.
- Avoid filler words and full-sentence rewrites.
- Return unique items only.
- If the query is already short and specific, reuse its key phrases directly.
"""

        try:
            response = await self.openai_client.responses.parse(
                model=self.model,
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": query},
                ],
                text_format=JournalKeywordExtraction,
            )
            parsed = response.output_parsed or JournalKeywordExtraction()
            normalized = self._normalize_keywords(parsed.keywords, max_keywords=max_keywords)
            self.logger.debug(
                "LLM keyword extraction success: query=%r keywords=%s",
                query,
                normalized,
            )
            return normalized
        except Exception as exc:
            self.logger.error("Error extracting journal search keywords: %s", exc)
            fallback = self._fallback_keywords(query, max_keywords=max_keywords)
            self.logger.debug(
                "LLM keyword extraction fallback: query=%r keywords=%s",
                query,
                fallback,
            )
            return fallback

    @staticmethod
    def _normalize_keywords(keywords: List[str], max_keywords: int = 5) -> List[str]:
        normalized: List[str] = []
        seen: set[str] = set()
        for keyword in keywords:
            item = " ".join(str(keyword or "").split()).strip()
            if not item:
                continue
            key = item.casefold()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(item)
            if len(normalized) >= max_keywords:
                break
        return normalized

    def _fallback_keywords(self, query: str, max_keywords: int = 5) -> List[str]:
        chunks = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9][A-Za-z0-9_:/.-]*", query)
        if not chunks:
            self.logger.debug("Keyword fallback produced raw-query slice for query=%r", query)
            return [query[:40]]

        chunks.sort(key=len, reverse=True)
        normalized = self._normalize_keywords(chunks, max_keywords=max_keywords)
        self.logger.debug("Keyword fallback regex extraction: query=%r keywords=%s", query, normalized)
        return normalized

    @classmethod
    def _normalize_journal_content(cls, content: str) -> str:
        lines: List[str] = []
        previous_blank = False
        for raw_line in str(content or "").splitlines():
            normalized_line = " ".join(raw_line.split()).strip()
            if not normalized_line:
                if lines and not previous_blank:
                    lines.append("")
                previous_blank = True
                continue
            lines.append(normalized_line)
            previous_blank = False

        while lines and lines[0] == "":
            lines.pop(0)
        while lines and lines[-1] == "":
            lines.pop()
        return "\n".join(lines)
