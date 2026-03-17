import logging
from datetime import datetime

from openai import AsyncOpenAI

from ....schemas.memory import MemoryExtraction


class MemoryLLMService:
    """LLM service for extracting durable memory pieces from conversation history."""

    def __init__(self, model: str = "gpt-5-mini"):
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        self.openai_client = AsyncOpenAI()
        self.model = model

    async def extract_memories_from_content(self, content: str) -> MemoryExtraction:
        """Extract profile and episodic memories from raw conversation text."""
        current_date = datetime.now().strftime("%Y-%m-%d")

        system_prompt = f"""You are an expert memory extraction system. Extract only durable, useful long-term memories from the conversation.

CURRENT DATE: {current_date}

Rules:
- Be selective. Do not store trivial chatter.
- Keep memories in the user's original language.
- Extract only PROFILE and EPISODIC memories.
- PROFILE: stable preferences, traits, identity, working style, recurring habits.
- EPISODIC: meaningful events, commitments, plans, and experiences.
- Store only information that can help in future conversations.
- Skip fleeting requests, temporary logistics, and one-off small talk unless they create a durable commitment or constraint.
- Preserve important dates and times when they matter.
- If a fact belongs to a specific speaker, include that speaker's sender_id explicitly in the memory content.
- If multiple people appear, keep the speaker identity explicit instead of writing anonymous memories.
"""

        user_prompt = f"""Conversation:
{content}

Extract meaningful long-term memories from this conversation."""

        try:
            response = await self.openai_client.responses.parse(
                model=self.model,
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                text_format=MemoryExtraction
            )
            return response.output_parsed or MemoryExtraction(memories=[])
        except Exception as exc:
            self.logger.error("Error extracting memories: %s", exc)
            return MemoryExtraction(memories=[])
