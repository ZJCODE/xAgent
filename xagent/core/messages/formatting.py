"""Model-facing formatting for persisted experience."""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from ..config import AgentConfig
from ...schemas import Message, MessageType, RoleType
from ...schemas.attachment import ATTACHMENT_METADATA_KEY


class ExperienceFormatter:
    """Render conversation messages and context events into prompt-safe text."""

    @classmethod
    def build_recent_transcript_message(
        cls,
        messages: List[Message],
        current_user_id: str,
        memory_context: str = "",
        context_events: Optional[List[Message]] = None,
        max_messages: int = AgentConfig.DEFAULT_MAX_HISTORY,
        max_context_events: int = AgentConfig.MAX_CONTEXT_EVENTS,
    ) -> dict:
        conversation_messages = cls.filter_conversation_messages(messages)
        observation_messages = (
            cls.filter_context_events(messages)
            if context_events is None
            else cls.filter_context_events(context_events)
        )
        budgeted_entries, omitted_count = cls.budget_transcript_entries(
            conversation_messages,
            max_messages=max_messages,
        )
        budgeted_observations, omitted_observation_count = cls.budget_context_events(
            observation_messages,
            max_events=max_context_events,
        )
        experience_entries = cls.merge_experience_entries(
            budgeted_entries,
            budgeted_observations,
        )

        transcript_lines: list[str] = [
            AgentConfig.DEFAULT_SYSTEM_PROMPT.rstrip(),
            f"- Current speaker: {current_user_id}",
            f"- Date: {datetime.now().strftime('%Y-%m-%d')}",
            "",
        ]

        if memory_context:
            transcript_lines.append(
                "**Recent Memory** "
                "(attribution rules per instructions):\n\n"
                + memory_context
            )
            transcript_lines.append("")

        transcript_lines.extend([
            "==========\n",
            "",
            "**Recent Experience** (conversation and observations in chronological order):",
            "",
        ])

        if omitted_count or omitted_observation_count:
            transcript_lines.append(
                cls.format_omitted_experience_note(
                    omitted_messages=omitted_count,
                    omitted_observations=omitted_observation_count,
                )
            )
            transcript_lines.append("")

        for entry_type, msg, content in experience_entries:
            transcript_lines.extend(cls.format_experience_entry(entry_type, msg, content))
            transcript_lines.append("")

        transcript_lines.append(AgentConfig.build_turn_reply_prompt(current_user_id))
        return {"role": RoleType.USER.value, "content": "\n".join(transcript_lines).strip()}

    @classmethod
    def build_recent_experience_context(
        cls,
        experience_entries: List[tuple[str, Message, str]],
        omitted_messages: int,
        omitted_observations: int,
    ) -> str:
        lines: list[str] = []
        if omitted_messages or omitted_observations:
            lines.append(
                cls.format_omitted_experience_note(
                    omitted_messages=omitted_messages,
                    omitted_observations=omitted_observations,
                )
            )
            lines.append("")

        for entry_type, msg, content in experience_entries:
            lines.extend(cls.format_experience_entry(entry_type, msg, content))
            lines.append("")

        experience_text = "\n".join(lines).strip() or "[No recent experience]"
        return cls.wrap_untrusted_context(AgentConfig.RECENT_EXPERIENCE_NAME, experience_text)

    @staticmethod
    def wrap_untrusted_context(tag_name: str, content: str) -> str:
        return (
            f"<{tag_name}>\n\n"
            f"{content.strip()}\n\n"
            f"</{tag_name}>"
        )

    @staticmethod
    def filter_conversation_messages(messages: List[Message]) -> List[Message]:
        return [
            msg for msg in messages
            if msg.type == MessageType.MESSAGE
            and msg.role in (RoleType.USER, RoleType.ASSISTANT)
        ]

    @staticmethod
    def filter_context_events(messages: List[Message]) -> List[Message]:
        return [msg for msg in messages if msg.type == MessageType.CONTEXT_EVENT]

    @staticmethod
    def merge_experience_entries(
        conversation_entries: List[tuple[Message, str]],
        observation_entries: List[tuple[Message, str]],
    ) -> List[tuple[str, Message, str]]:
        entries = [("message", msg, content) for msg, content in conversation_entries]
        entries.extend(("observation", msg, content) for msg, content in observation_entries)
        return sorted(entries, key=lambda entry: entry[1].timestamp)

    @staticmethod
    def format_omitted_experience_note(
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

    @classmethod
    def format_experience_entry(
        cls,
        entry_type: str,
        message: Message,
        content: str,
    ) -> List[str]:
        if entry_type == "observation":
            return [cls.format_context_event_header(message), content]

        lines = [cls.format_transcript_message_header(message), content]
        image_count = cls.count_message_images(message)
        if image_count:
            noun = "image" if image_count == 1 else "images"
            lines.append(f"[Attached {noun}: {image_count}]")
        attachment_count = cls.count_message_attachments(message)
        if attachment_count and attachment_count != image_count:
            noun = "file" if attachment_count == 1 else "files"
            lines.append(f"[Attached {noun}: {attachment_count}]")
        return lines

    @staticmethod
    def budget_context_events(
        messages: List[Message],
        max_events: int,
    ) -> tuple[List[tuple[Message, str]], int]:
        if not messages:
            return [], 0

        event_limit = max(1, int(max_events or AgentConfig.MAX_CONTEXT_EVENTS))
        omitted_count = max(0, len(messages) - event_limit)
        selected = messages[-event_limit:]
        return [
            (
                msg,
                msg.content.strip() or "[Empty observation]",
            )
            for msg in selected
        ], omitted_count

    @classmethod
    def format_context_event_header(cls, message: Message) -> str:
        return f"[ambient context][timestamp={cls.format_transcript_timestamp(message)}]"

    @classmethod
    def format_transcript_message_header(cls, message: Message) -> str:
        speaker = cls.format_transcript_speaker(message)
        timestamp = cls.format_transcript_timestamp(message)
        return f"[speaker={speaker}][timestamp={timestamp}]"

    @staticmethod
    def format_transcript_timestamp(message: Message) -> str:
        return datetime.fromtimestamp(message.timestamp).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def budget_transcript_entries(
        messages: List[Message],
        max_messages: int,
    ) -> tuple[List[tuple[Message, str]], int]:
        if not messages:
            return [], 0

        message_limit = max(1, int(max_messages or AgentConfig.DEFAULT_MAX_HISTORY))
        omitted_count = max(0, len(messages) - message_limit)
        candidates = messages[-message_limit:]
        return [
            (msg, msg.content.strip() or "[Empty message]")
            for msg in candidates
        ], omitted_count

    @staticmethod
    def count_message_images(message: Message) -> int:
        metadata_images = message.metadata.get("images") if isinstance(message.metadata, dict) else None
        if isinstance(metadata_images, list):
            return len(metadata_images)
        if not message.images:
            return 0
        return len(message.images)

    @staticmethod
    def count_message_attachments(message: Message) -> int:
        metadata_attachments = message.metadata.get(ATTACHMENT_METADATA_KEY) if isinstance(message.metadata, dict) else None
        return len(metadata_attachments) if isinstance(metadata_attachments, list) else 0

    @staticmethod
    def format_transcript_speaker(message: Message) -> str:
        if message.role == RoleType.ASSISTANT:
            return "ME"
        return message.sender_id or message.role.value

    @staticmethod
    def latest_current_user_message(
        messages: List[Message],
        current_user_id: str,
    ) -> Optional[Message]:
        if not messages:
            return None
        message = messages[-1]
        if message.role == RoleType.USER and message.sender_id == current_user_id:
            return message
        return None
