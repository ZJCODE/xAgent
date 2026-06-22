"""Per-turn dynamic context construction."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import List, Optional, Union

from .message_formatting import ExperienceFormatter
from .message_images import MessageImageNormalizer
from ..config.schema import AgentConfig
from ..domain import Message, RoleType


class TurnContextBuilder:
    """Build dynamic user-message layers for one model turn."""

    def __init__(self, workspace_dir: Optional[Union[str, Path]] = None) -> None:
        self.workspace_dir = workspace_dir

    def build_messages(
        self,
        messages: List[Message],
        current_user_id: str,
        memory_context: str = "",
        context_events: Optional[List[Message]] = None,
        current_time: Optional[str] = None,
        current_date: Optional[str] = None,
        max_messages: int = AgentConfig.DEFAULT_MAX_HISTORY,
        max_context_events: int = AgentConfig.MAX_CONTEXT_EVENTS,
        include_images: bool = True,
        current_message: Optional[Message] = None,
        channel_instructions: str = "",
    ) -> list[dict]:
        conversation_messages = ExperienceFormatter.filter_conversation_messages(messages)
        observation_messages = (
            ExperienceFormatter.filter_context_events(messages)
            if context_events is None
            else ExperienceFormatter.filter_context_events(context_events)
        )
        budgeted_entries, omitted_count = ExperienceFormatter.budget_transcript_entries(
            conversation_messages,
            max_messages=max_messages,
        )
        budgeted_observations, omitted_observation_count = ExperienceFormatter.budget_context_events(
            observation_messages,
            max_events=max_context_events,
        )
        experience_entries = ExperienceFormatter.merge_experience_entries(
            budgeted_entries,
            budgeted_observations,
        )

        context_messages: list[dict] = []
        if memory_context.strip():
            context_messages.append({
                "role": RoleType.USER.value,
                "name": AgentConfig.RECENT_MEMORY_NAME,
                "content": ExperienceFormatter.wrap_untrusted_context(
                    AgentConfig.RECENT_MEMORY_NAME,
                    memory_context,
                ),
            })

        context_messages.append({
            "role": RoleType.USER.value,
            "name": AgentConfig.RECENT_EXPERIENCE_NAME,
            "content": ExperienceFormatter.build_recent_experience_context(
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
            channel_instructions=channel_instructions,
        )
        current_task_message = {
            "role": RoleType.USER.value,
            "name": AgentConfig.CURRENT_TASK_NAME,
            "content": current_task_text,
        }

        current_images: List[str] = []
        if include_images:
            image_message = current_message or ExperienceFormatter.latest_current_user_message(
                conversation_messages,
                current_user_id,
            )
            current_images = MessageImageNormalizer.current_message_images(
                image_message,
                current_user_id,
                workspace_dir=self.workspace_dir,
            )
        if current_images:
            content = [{"type": "text", "text": current_task_text}]
            content.extend(
                {"type": "image_url", "image_url": {"url": image_source}}
                for image_source in current_images
            )
            current_task_message["content"] = content

        context_messages.append(current_task_message)
        return context_messages
