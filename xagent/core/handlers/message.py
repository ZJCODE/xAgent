import time
import logging
from typing import List, Optional, Union

from ..config import AgentConfig
from ...components import MessageStorageBase
from ...schemas import Message, RoleType, MessageType
from ...utils.image_utils import extract_image_urls_from_text

logger = logging.getLogger(__name__)


class MessageHandler:
    """Handles message storage, retrieval, sanitization, and system prompt building."""

    def __init__(
        self,
        message_storage: MessageStorageBase,
        system_prompt: str = "",
    ):
        self.message_storage = message_storage
        self.system_prompt = system_prompt

    async def store_user_message(
        self,
        user_message: str,
        user_id: str,
        image_source: Optional[Union[str, List[str]]] = None,
    ) -> None:
        """Store a user message, auto-detecting embedded image URLs."""
        detected = extract_image_urls_from_text(user_message)
        if detected:
            existing = []
            if image_source:
                existing = image_source if isinstance(image_source, list) else [image_source]
            merged = list(dict.fromkeys(existing + detected))
            image_source = merged

        msg = Message.create(
            content=user_message,
            role=RoleType.USER,
            image_source=image_source,
            sender_id=user_id,
        )
        await self.message_storage.add_messages(msg)

    async def store_model_reply(self, reply_text: str, sender_id: str) -> None:
        model_msg = Message.create(content=reply_text, role=RoleType.ASSISTANT, sender_id=sender_id)
        await self.message_storage.add_messages(model_msg)

    async def get_recent_messages(
        self,
        history_count: int,
    ) -> List[Message]:
        return await self.message_storage.get_messages(history_count)

    async def get_input_messages(
        self,
        history_count: int,
    ) -> list:
        """Retrieve and serialize recent messages for model input."""
        messages = await self.get_recent_messages(history_count)
        return [msg.to_model_input() for msg in messages]

    @staticmethod
    def to_model_input(messages: List[Message]) -> list:
        return [msg.to_model_input() for msg in messages]

    def build_system_prompt(
        self,
        user_id: str,
        memory_context: str = "",
        tool_names: Optional[List[str]] = None,
    ) -> str:
        """Build the runtime system prompt.

        Prompt layering order (each section only included when relevant):
          1. Core Principles — foundational behaviour guidelines
          2. Tool Instructions — per-tool safety / usage rules
          3. Context Information — runtime metadata (speaker, date)
          4. Recent Diary Memory — recent daily diary entries (only when non-empty)
          5. User System Prompt — developer-supplied customisation
          (6. User Message — appended as normal messages, not part of system prompt)
        """
        sections: list[str] = []

        # --- 1. Core Principles ---
        sections.append(AgentConfig.BASE_AGENT_PROMPT)

        # --- 2. Tool Instructions ---
        seen: set[str] = set()
        for name in (tool_names or []):
            if name in seen:
                continue
            seen.add(name)
            segment = AgentConfig.TOOL_SYSTEM_PROMPTS.get(name)
            if segment:
                sections.append(segment)

        # --- 3. Context Information ---
        context_lines = [
            AgentConfig.DEFAULT_SYSTEM_PROMPT.rstrip(),
            f"- Current speaker: {user_id}",
            "- Recent messages come from the agent's continuous global interaction stream and may mix multiple user_ids.",
            f"- Date: {time.strftime('%Y-%m-%d')}",
        ]
        sections.append("\n".join(context_lines))

        # --- 4. Recent Diary Memory (conditional) ---
        if memory_context:
            sections.append(
                "**Recent Diary Memory:**\n"
                "- These are your recent diary entries. If they conflict with the recent transcript, trust the recent transcript.\n\n"
                + memory_context
            )

        # --- 5. Developer-supplied system prompt ---
        if self.system_prompt:
            sections.append(self.system_prompt)

        prompt = "\n\n".join(sections)

        if len(prompt) > AgentConfig.MAX_SYSTEM_PROMPT_LENGTH:
            logger.warning(
                "System prompt length (%d chars) exceeds soft limit (%d). "
                "Consider reducing memory results or shortening the user system prompt.",
                len(prompt), AgentConfig.MAX_SYSTEM_PROMPT_LENGTH,
            )

        return prompt

    @staticmethod
    def sanitize_input_messages(input_messages: list) -> list:
        """Remove leading function call output messages."""
        while input_messages and input_messages[0].get("type") == MessageType.FUNCTION_CALL_OUTPUT:
            input_messages.pop(0)
        return input_messages

    @staticmethod
    def filter_non_tool_messages(messages: list) -> list:
        """Filter messages to only user and assistant roles."""
        return [
            msg for msg in messages
            if msg.get("role") in (RoleType.USER.value, RoleType.ASSISTANT.value)
        ]
