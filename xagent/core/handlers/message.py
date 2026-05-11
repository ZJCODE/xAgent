import time
import logging
from typing import Any, Dict, List, Optional, Union

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
    ) -> Message:
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
        return msg

    async def store_model_reply(self, reply_text: str, sender_id: str) -> Message:
        model_msg = Message.create(content=reply_text, role=RoleType.ASSISTANT, sender_id=sender_id)
        await self.message_storage.add_messages(model_msg)
        return model_msg

    async def store_context_event(
        self,
        context: str,
        source: str = "environment",
        event_type: str = "observation",
        sender_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Message:
        """Store a non-direct observation from the agent's environment."""
        event_msg = Message.create_context_event(
            content=context,
            source=source,
            event_type=event_type,
            sender_id=sender_id,
            metadata=metadata,
        )
        await self.message_storage.add_messages(event_msg)
        return event_msg

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

    @staticmethod
    def filter_conversation_messages(messages: List[Message]) -> List[Message]:
        """Keep only persisted user/assistant natural-language messages."""
        return [
            msg for msg in messages
            if msg.type == MessageType.Message
            and msg.role in (RoleType.USER, RoleType.ASSISTANT)
        ]

    @staticmethod
    def filter_context_events(messages: List[Message]) -> List[Message]:
        """Keep persisted environment observations/context events."""
        return [msg for msg in messages if msg.type == MessageType.CONTEXT_EVENT]

    @staticmethod
    def build_recent_transcript_message(
        messages: List[Message],
        current_user_id: str,
        memory_context: str = "",
        turn_kind: str = "chat",
        context_events: Optional[List[Message]] = None,
        max_messages: int = AgentConfig.MAX_TRANSCRIPT_MESSAGES,
        max_total_chars: int = AgentConfig.MAX_TRANSCRIPT_CHARS,
        max_message_chars: int = AgentConfig.MAX_TRANSCRIPT_MESSAGE_CHARS,
        max_context_events: int = AgentConfig.MAX_CONTEXT_EVENTS,
        max_context_event_chars: int = AgentConfig.MAX_CONTEXT_EVENT_CHARS,
    ) -> dict:
        """Collapse recent conversation history into one user transcript message.

                Includes per-turn dynamic context that changes each call:
                    - Runtime metadata (date, and current speaker for direct chat turns)
          - Recent diary memory (conditional)
                    - Recent experience in chronological order
        """
        conversation_messages = MessageHandler.filter_conversation_messages(messages)
        observation_messages = (
            MessageHandler.filter_context_events(messages)
            if context_events is None
            else MessageHandler.filter_context_events(context_events)
        )
        budgeted_entries, omitted_count = MessageHandler._budget_transcript_entries(
            conversation_messages,
            max_messages=max_messages,
            max_total_chars=max_total_chars,
            max_message_chars=max_message_chars,
        )
        budgeted_messages = [msg for msg, _ in budgeted_entries]
        budgeted_observations, omitted_observation_count = MessageHandler._budget_context_events(
            observation_messages,
            max_events=max_context_events,
            max_event_chars=max_context_event_chars,
        )
        experience_entries = MessageHandler._merge_experience_entries(
            budgeted_entries,
            budgeted_observations,
        )

        transcript_lines: list[str] = []

        # --- Runtime context ---
        transcript_lines.append(AgentConfig.DEFAULT_SYSTEM_PROMPT.rstrip())
        if turn_kind != "observe":
            transcript_lines.append(f"- Current speaker: {current_user_id}")
        transcript_lines.append(f"- Date: {time.strftime('%Y-%m-%d')}")
        transcript_lines.append("")

        # --- Recent diary memory (conditional) ---
        if memory_context:
            transcript_lines.append(
                "**Recent Diary Memory** "
                "(attribution rules per instructions):\n\n"
                + memory_context
            )
            transcript_lines.append("")

        # --- Recent experience ---
        transcript_lines.append("==========\n")
        transcript_lines.append("")
        transcript_lines.append("**Recent Experience** (conversation and observations in chronological order):")
        transcript_lines.append("")

        if omitted_count or omitted_observation_count:
            transcript_lines.append(
                MessageHandler._format_omitted_experience_note(
                    omitted_messages=omitted_count,
                    omitted_observations=omitted_observation_count,
                )
            )
            transcript_lines.append("")

        for entry_type, msg, content in experience_entries:
            transcript_lines.extend(
                MessageHandler._format_experience_entry(entry_type, msg, content)
            )
            transcript_lines.append("")

        transcript_lines.append(MessageHandler._build_turn_instruction(turn_kind, current_user_id))

        transcript_text = "\n".join(transcript_lines).strip()

        print("=== Built transcript message content ===")
        print(transcript_text)
        print("=== End transcript message content ===")

        latest_images = MessageHandler._latest_user_images(budgeted_messages, current_user_id)
        if not latest_images:
            return {"role": RoleType.USER.value, "content": transcript_text}

        content = [{"type": "text", "text": transcript_text}]
        content.extend(
            {"type": "image_url", "image_url": {"url": image_source}}
            for image_source in latest_images
        )
        return {"role": RoleType.USER.value, "content": content}

    @staticmethod
    def _merge_experience_entries(
        conversation_entries: List[tuple[Message, str]],
        observation_entries: List[tuple[Message, str]],
    ) -> List[tuple[str, Message, str]]:
        entries = [
            ("message", msg, content)
            for msg, content in conversation_entries
        ]
        entries.extend(
            ("observation", msg, content)
            for msg, content in observation_entries
        )
        return sorted(entries, key=lambda entry: entry[1].timestamp)

    @staticmethod
    def _format_omitted_experience_note(
        omitted_messages: int,
        omitted_observations: int,
    ) -> str:
        parts: list[str] = []
        if omitted_messages:
            noun = "message" if omitted_messages == 1 else "messages"
            parts.append(f"{omitted_messages} conversation {noun}")
        if omitted_observations:
            noun = "observation" if omitted_observations == 1 else "observations"
            parts.append(f"{omitted_observations} {noun}")
        return "[Earlier experience omitted: " + ", ".join(parts) + "]"

    @staticmethod
    def _format_experience_entry(
        entry_type: str,
        message: Message,
        content: str,
    ) -> List[str]:
        if entry_type == "observation":
            return [MessageHandler._format_context_event_header(message), content]

        lines = [
            f"[speaker={MessageHandler._format_transcript_speaker(message)}]",
            content,
        ]
        image_count = MessageHandler._count_message_images(message)
        if image_count:
            noun = "image" if image_count == 1 else "images"
            lines.append(f"[Attached {noun}: {image_count}]")
        return lines

    @staticmethod
    def _budget_context_events(
        messages: List[Message],
        max_events: int,
        max_event_chars: int,
    ) -> tuple[List[tuple[Message, str]], int]:
        if not messages:
            return [], 0

        event_limit = max(1, int(max_events or AgentConfig.MAX_CONTEXT_EVENTS))
        per_event_limit = max(1, int(max_event_chars or AgentConfig.MAX_CONTEXT_EVENT_CHARS))
        omitted_count = max(0, len(messages) - event_limit)
        selected = messages[-event_limit:]
        return [
            (
                msg,
                MessageHandler._truncate_transcript_content(
                    msg.content.strip() or "[Empty observation]",
                    per_event_limit,
                ),
            )
            for msg in selected
        ], omitted_count

    @staticmethod
    def _format_context_event_header(message: Message) -> str:
        return "[ambient context]"

    @staticmethod
    def _build_turn_instruction(turn_kind: str, current_user_id: str) -> str:
        if turn_kind == "observe":
            return (
                "\n==========\n\nYou have just received an observation from the environment. "
                "Decide whether speaking is useful, timely, and socially appropriate. "
                "If silence is better, set replied to false and reply to null. "
                "If you should speak, set replied to true and provide only the words you would say. "
                "Never mention internal labels, observations, metadata, tags, or message formatting in your reply."
            )
        return (
            "\n==========\n\nNow reply directly to the latest message "
            f"from {current_user_id}. Respond as yourself — do not suggest, "
            "propose alternatives, or wrap your reply in quotes. "
            "Never mention internal labels, tags, or message formatting in your reply."
        )

    @staticmethod
    def _budget_transcript_entries(
        messages: List[Message],
        max_messages: int,
        max_total_chars: int,
        max_message_chars: int,
    ) -> tuple[List[tuple[Message, str]], int]:
        if not messages:
            return [], 0

        message_limit = max(1, int(max_messages or AgentConfig.MAX_TRANSCRIPT_MESSAGES))
        total_limit = max(1, int(max_total_chars or AgentConfig.MAX_TRANSCRIPT_CHARS))
        per_message_limit = max(1, int(max_message_chars or AgentConfig.MAX_TRANSCRIPT_MESSAGE_CHARS))

        omitted_count = max(0, len(messages) - message_limit)
        candidates = messages[-message_limit:]
        selected_reversed: list[tuple[Message, str]] = []
        used_chars = 0

        for index in range(len(candidates) - 1, -1, -1):
            msg = candidates[index]
            content = MessageHandler._truncate_transcript_content(
                msg.content.strip() or "[Empty message]",
                per_message_limit,
            )
            estimated_chars = MessageHandler._estimate_transcript_entry_chars(msg, content)
            if selected_reversed and used_chars + estimated_chars > total_limit:
                omitted_count += index + 1
                break
            selected_reversed.append((msg, content))
            used_chars += estimated_chars

        return list(reversed(selected_reversed)), omitted_count

    @staticmethod
    def _truncate_transcript_content(content: str, limit: int) -> str:
        if len(content) <= limit:
            return content
        omitted_chars = len(content) - limit
        clipped = content[:limit].rstrip()
        return f"{clipped}\n[Content truncated: {omitted_chars} chars omitted]"

    @staticmethod
    def _estimate_transcript_entry_chars(message: Message, content: str) -> int:
        speaker = MessageHandler._format_transcript_speaker(message)
        image_note_chars = 0
        image_count = MessageHandler._count_message_images(message)
        if image_count:
            image_note_chars = len(f"[Attached images: {image_count}]")
        return len(speaker) + len(content) + image_note_chars + 8

    @staticmethod
    def _count_message_images(message: Message) -> int:
        if not message.multimodal or not message.multimodal.image:
            return 0
        images = message.multimodal.image
        return len(images) if isinstance(images, list) else 1

    @staticmethod
    def _format_transcript_speaker(message: Message) -> str:
        if message.role == RoleType.ASSISTANT:
            return "you"
        return message.sender_id or message.role.value

    @staticmethod
    def _latest_user_images(messages: List[Message], current_user_id: str) -> List[str]:
        for msg in reversed(messages):
            if msg.role != RoleType.USER or msg.sender_id != current_user_id:
                continue
            if not msg.multimodal or not msg.multimodal.image:
                return []

            images = msg.multimodal.image
            image_items = images if isinstance(images, list) else [images]
            return [image.source for image in image_items if image.source]
        return []

    def build_instructions(
        self,
        tool_names: Optional[List[str]] = None,
    ) -> str:
        """Build the static instructions string for the model.

        Contains only behavioural rules that do not change per-turn:
          1. Core Principles — foundational behaviour guidelines
          2. Tool Instructions — per-tool safety / usage rules
          3. User System Prompt — developer-supplied customisation
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

        # --- 3. Developer-supplied system prompt ---
        if self.system_prompt:
            sections.append(self.system_prompt)

        instructions = "\n\n".join(sections)

        if len(instructions) > AgentConfig.MAX_SYSTEM_PROMPT_LENGTH:
            logger.warning(
                "Instructions length (%d chars) exceeds soft limit (%d). "
                "Consider shortening the user system prompt.",
                len(instructions), AgentConfig.MAX_SYSTEM_PROMPT_LENGTH,
            )

        return instructions

    @staticmethod
    def sanitize_input_messages(input_messages: list) -> list:
        """Remove leading tool result messages, which are invalid without a prior assistant tool call."""
        while input_messages and (
            input_messages[0].get("type") == MessageType.FUNCTION_CALL_OUTPUT
            or input_messages[0].get("role") == RoleType.TOOL.value
        ):
            input_messages.pop(0)
        return input_messages

    @staticmethod
    def filter_non_tool_messages(messages: list) -> list:
        """Filter messages to only user and assistant roles."""
        return [
            msg for msg in messages
            if msg.get("role") in (RoleType.USER.value, RoleType.ASSISTANT.value)
        ]
