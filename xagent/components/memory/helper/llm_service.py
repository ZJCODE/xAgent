import logging
from datetime import datetime

from openai import AsyncOpenAI

from ....schemas.memory import MemoryExtraction


class MemoryLLMService:
    """LLM service for extracting durable memory pieces from message history."""

    def __init__(self, model: str = "gpt-5-mini"):
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        self.openai_client = AsyncOpenAI()
        self.model = model

    async def extract_memories_from_content(self, content: str) -> MemoryExtraction:
        """Extract durable long-term memories from raw transcript text."""
        current_date = datetime.now().strftime("%Y-%m-%d")

        system_prompt = f"""You are an expert memory extraction system. Extract only durable, useful long-term memories from the transcript chunk.

CURRENT DATE: {current_date}

Rules:
- Be selective. Do not store trivial chatter.
- Keep memories in the transcript's original language whenever possible.
- Extract only these memory types:
  - EPISODIC: important dated events, commitments, decisions, plans, and experiences.
  - SEMANTIC: stable facts, roles, preferences, priorities, responsibilities, and recurring patterns.
  - SOCIAL: relationships, group membership, alignment, and working agreements.
  - SELF: the agent's own ongoing work state, strategy, commitments, and response-style adjustments.
- Prefer one strong SEMANTIC memory over many repetitive EPISODIC memories.
- Store only information that can help in future interactions.
- Skip fleeting requests, temporary logistics, and one-off small talk unless they create a durable commitment or constraint.
- Preserve important dates, times, and speaker identities when they matter.
- Keep speaker identities explicit in the memory content instead of writing anonymous memories.
"""

        user_prompt = f"""Transcript:
{content}

Extract meaningful long-term memories from this transcript chunk."""

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
