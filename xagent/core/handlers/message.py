import time
from typing import List, Optional, Union

from ..config import AgentConfig
from ...components import MessageStorageBase
from ...schemas import Message, RoleType, MessageType
from ...utils.image_utils import extract_image_urls_from_text


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
    ) -> str:
        """Build the runtime system prompt."""
        sections = [
            AgentConfig.DEFAULT_SYSTEM_PROMPT.rstrip(),
            f"- Current user_id: {user_id}",
            f"- Current date: {time.strftime('%Y-%m-%d')}",
            f"- Current timezone: {time.tzname[0]}",
            "",
            f"- Retrieve relevant memories for user: {retrieved_memories or 'No relevant memories found.'}",
            "",
            f"- Shared context: {shared_context or 'No shared context.'}",
        ]
        if self.system_prompt:
            sections.extend(["", self.system_prompt])
        return "\n".join(sections)

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
