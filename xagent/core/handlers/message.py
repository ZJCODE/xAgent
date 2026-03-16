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
        session_id: str,
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

        msg = Message.create(content=user_message, role=RoleType.USER, image_source=image_source)
        await self.message_storage.add_messages(user_id, session_id, msg)

    async def store_model_reply(self, reply_text: str, user_id: str, session_id: str) -> None:
        model_msg = Message.create(content=reply_text, role=RoleType.ASSISTANT)
        await self.message_storage.add_messages(user_id, session_id, model_msg)

    async def get_input_messages(self, user_id: str, session_id: str, history_count: int) -> list:
        """Retrieve and serialize recent messages for model input."""
        messages = await self.message_storage.get_messages(user_id, session_id, history_count)
        return [msg.to_dict() for msg in messages]

    def build_system_prompt(
        self,
        user_id: str,
        retrieved_memories: Optional[List[dict]] = None,
        shared_context: Optional[str] = None,
        tool_names: Optional[List[str]] = None,
    ) -> str:
        """Build the runtime system prompt.

        Prompt layering order (each section only included when relevant):
          1. Core Principles — foundational behaviour guidelines
          2. Tool Instructions — per-tool safety / usage rules
          3. Context Information — runtime metadata (user, date, timezone, shared context)
          4. Retrieved Memories — relevant memories (only when non-empty)
          5. User System Prompt — developer-supplied customisation
          (6. User Message — appended as conversation messages, not part of system prompt)
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
            f"- Current user: {user_id}",
            f"- Date: {time.strftime('%Y-%m-%d')}",
            f"- Timezone: {time.tzname[0]}",
        ]
        if shared_context:
            context_lines.append(f"- Shared context: {shared_context}")
        sections.append("\n".join(context_lines))

        # --- 4. Retrieved Memories (conditional) ---
        memory_block = self._format_memories(retrieved_memories)
        if memory_block:
            sections.append(memory_block)

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
    def _format_memories(retrieved_memories: Optional[List[dict]]) -> str:
        """Format retrieved memories into a structured prompt section.

        Returns an empty string when there are no memories so the caller
        can skip appending the section entirely.
        """
        if not retrieved_memories:
            return ""

        lines = ["**Retrieved Memories:**"]
        for i, mem in enumerate(retrieved_memories, 1):
            content = mem.get("content", "") if isinstance(mem, dict) else str(mem)
            if not content:
                continue
            metadata = mem.get("metadata", {}) if isinstance(mem, dict) else {}
            mem_type = metadata.get("type", "")
            prefix = f"[{mem_type}] " if mem_type else ""
            lines.append(f"{i}. {prefix}{content}")

        return "\n".join(lines) if len(lines) > 1 else ""

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
