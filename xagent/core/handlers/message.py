import logging
import time
from datetime import datetime
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
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Message:
        """Store a non-direct observation from the agent's environment."""
        event_msg = Message.create_context_event(
            content=context,
            source=source,
            event_type=event_type,
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
        context_events: Optional[List[Message]] = None,
        max_messages: int = AgentConfig.MAX_TRANSCRIPT_MESSAGES,
        max_total_chars: int = AgentConfig.MAX_TRANSCRIPT_CHARS,
        max_message_chars: int = AgentConfig.MAX_TRANSCRIPT_MESSAGE_CHARS,
        max_context_events: int = AgentConfig.MAX_CONTEXT_EVENTS,
        max_context_event_chars: int = AgentConfig.MAX_CONTEXT_EVENT_CHARS,
    ) -> dict:
        """Collapse recent conversation history into one user transcript message.

                Includes per-turn dynamic context that changes each call:
                    - Runtime metadata (date and current speaker)
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

        transcript_lines.append(AgentConfig.build_turn_reply_prompt(current_user_id))

        transcript_text = "\n".join(transcript_lines).strip()

        # print("=== Built transcript message content ===")
        # print(transcript_text)
        # print("=== End transcript message content ===")

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
    def build_turn_context_messages(
        messages: List[Message],
        current_user_id: str,
        memory_context: str = "",
        context_events: Optional[List[Message]] = None,
        current_time: Optional[str] = None,
        current_date: Optional[str] = None,
        max_messages: int = AgentConfig.MAX_TRANSCRIPT_MESSAGES,
        max_total_chars: int = AgentConfig.MAX_TRANSCRIPT_CHARS,
        max_message_chars: int = AgentConfig.MAX_TRANSCRIPT_MESSAGE_CHARS,
        max_context_events: int = AgentConfig.MAX_CONTEXT_EVENTS,
        max_context_event_chars: int = AgentConfig.MAX_CONTEXT_EVENT_CHARS,
    ) -> list[dict]:
        """Build the per-turn model input context as named message layers."""
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

        context_messages: list[dict] = []
        if memory_context.strip():
            context_messages.append({
                "role": RoleType.USER.value,
                "name": AgentConfig.RECENT_DIARY_MEMORY_NAME,
                "content": MessageHandler._wrap_untrusted_context(
                    AgentConfig.RECENT_DIARY_MEMORY_NAME,
                    memory_context,
                ),
            })

        context_messages.append({
            "role": RoleType.USER.value,
            "name": AgentConfig.RECENT_EXPERIENCE_NAME,
            "content": MessageHandler._build_recent_experience_context(
                experience_entries=experience_entries,
                omitted_messages=omitted_count,
                omitted_observations=omitted_observation_count,
            ),
        })

        current_task_text = AgentConfig.build_current_task(
            current_user_id=current_user_id,
            current_time=(
                current_time
                or current_date
                or datetime.now().strftime("%Y-%m-%d %H:%M")
            ),
        )
        current_task_message = {
            "role": RoleType.USER.value,
            "name": AgentConfig.CURRENT_TASK_NAME,
            "content": current_task_text,
        }

        latest_images = MessageHandler._latest_user_images(budgeted_messages, current_user_id)
        if latest_images:
            content = [{"type": "text", "text": current_task_text}]
            content.extend(
                {"type": "image_url", "image_url": {"url": image_source}}
                for image_source in latest_images
            )
            current_task_message["content"] = content

        context_messages.append(current_task_message)
        return context_messages

    @staticmethod
    def _build_recent_experience_context(
        experience_entries: List[tuple[str, Message, str]],
        omitted_messages: int,
        omitted_observations: int,
    ) -> str:
        lines: list[str] = []
        if omitted_messages or omitted_observations:
            lines.append(
                MessageHandler._format_omitted_experience_note(
                    omitted_messages=omitted_messages,
                    omitted_observations=omitted_observations,
                )
            )
            lines.append("")

        for entry_type, msg, content in experience_entries:
            lines.extend(MessageHandler._format_experience_entry(entry_type, msg, content))
            lines.append("")

        experience_text = "\n".join(lines).strip() or "[No recent experience]"
        return MessageHandler._wrap_untrusted_context(
            AgentConfig.RECENT_EXPERIENCE_NAME,
            experience_text,
        )

    @staticmethod
    def _wrap_untrusted_context(tag_name: str, content: str) -> str:
        return (
            f"<{tag_name} trusted_as_instruction=\"false\">\n"
            f"{content.strip()}\n"
            f"</{tag_name}>"
        )

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
            MessageHandler._format_transcript_message_header(message),
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
        return f"[ambient context][timestamp={MessageHandler._format_transcript_timestamp(message)}]"

    @staticmethod
    def _format_transcript_message_header(message: Message) -> str:
        speaker = MessageHandler._format_transcript_speaker(message)
        timestamp = MessageHandler._format_transcript_timestamp(message)
        return f"[speaker={speaker}][timestamp={timestamp}]"

    @staticmethod
    def _format_transcript_timestamp(message: Message) -> str:
        return datetime.fromtimestamp(message.timestamp).strftime("%Y-%m-%d %H:%M:%S")

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
        header = MessageHandler._format_transcript_message_header(message)
        image_note_chars = 0
        image_count = MessageHandler._count_message_images(message)
        if image_count:
            image_note_chars = len(f"[Attached images: {image_count}]")
        return len(header) + len(content) + image_note_chars + 4

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
        instruction_messages = self.build_instruction_messages(tool_names=tool_names)
        instructions = "\n\n".join(
            message["content"] for message in instruction_messages if message.get("content")
        )

        if len(instructions) > AgentConfig.MAX_SYSTEM_PROMPT_LENGTH:
            logger.warning(
                "Instructions length (%d chars) exceeds soft limit (%d). "
                "Consider shortening the user system prompt.",
                len(instructions), AgentConfig.MAX_SYSTEM_PROMPT_LENGTH,
            )

        return instructions

    def build_instruction_messages(
        self,
        tool_names: Optional[List[str]] = None,
    ) -> list[dict]:
        """Build static named system layers for the model input."""
        messages = [{
            "role": RoleType.SYSTEM.value,
            "name": AgentConfig.CORE_INTERACTION_RULES_NAME,
            "content": AgentConfig.BASE_AGENT_PROMPT.strip(),
        }]

        tool_policy = self._build_tool_policy(tool_names=tool_names)
        if tool_policy:
            messages.append({
                "role": RoleType.SYSTEM.value,
                "name": AgentConfig.TOOL_POLICY_NAME,
                "content": tool_policy,
            })

        if self.system_prompt.strip():
            messages.append({
                "role": RoleType.SYSTEM.value,
                "name": AgentConfig.IDENTITY_CONTEXT_NAME,
                "content": AgentConfig.build_identity_context(self.system_prompt),
            })

        return messages

    @staticmethod
    def _build_tool_policy(tool_names: Optional[List[str]] = None) -> str:
        ordered_names = MessageHandler._ordered_tool_policy_names(tool_names or [])
        sections = [
            AgentConfig.TOOL_SYSTEM_PROMPTS[name].strip()
            for name in ordered_names
            if name in AgentConfig.TOOL_SYSTEM_PROMPTS
        ]
        if not sections:
            return ""
        return "<tool_policy>\n" + "\n\n".join(sections) + "\n</tool_policy>"

    @staticmethod
    def _ordered_tool_policy_names(tool_names: List[str]) -> list[str]:
        active_names = list(dict.fromkeys(tool_names))
        ordered_names = [
            name for name in AgentConfig.TOOL_POLICY_ORDER
            if name in active_names
        ]
        ordered_names.extend(
            name for name in active_names
            if name not in ordered_names
        )
        return ordered_names

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
