"""Static instruction-layer construction."""

from __future__ import annotations

import logging
from typing import List, Optional

from ..config import AgentConfig

logger = logging.getLogger(__name__)


class InstructionBuilder:
    """Build static model instruction layers."""

    def __init__(self, system_prompt: str = "") -> None:
        self.system_prompt = system_prompt

    def build_text(
        self,
        tool_names: Optional[List[str]] = None,
        skills_catalog: str = "",
        workspace_context: str = "",
    ) -> str:
        instruction_messages = self.build_messages(
            tool_names=tool_names,
            skills_catalog=skills_catalog,
            workspace_context=workspace_context,
        )
        instructions = "\n\n".join(
            message["content"] for message in instruction_messages if message.get("content")
        )

        if len(instructions) > AgentConfig.MAX_SYSTEM_PROMPT_LENGTH:
            logger.warning(
                "Instructions length (%d chars) exceeds soft limit (%d). "
                "Consider shortening the user system prompt.",
                len(instructions),
                AgentConfig.MAX_SYSTEM_PROMPT_LENGTH,
            )

        return instructions

    def build_messages(
        self,
        tool_names: Optional[List[str]] = None,
        skills_catalog: str = "",
        supports_vision: bool = True,
        workspace_context: str = "",
    ) -> list[dict]:
        core_prompt = AgentConfig.BASE_AGENT_PROMPT.strip()
        if not supports_vision:
            core_prompt = core_prompt + AgentConfig.NO_VISION_NOTICE.rstrip()
        messages = [{
            "role": "system",
            "name": AgentConfig.CORE_INTERACTION_RULES_NAME,
            "content": core_prompt,
        }]

        tool_policy = self.build_tool_policy(tool_names=tool_names)
        if tool_policy:
            messages.append({
                "role": "system",
                "name": AgentConfig.TOOL_POLICY_NAME,
                "content": tool_policy,
            })

        if self.system_prompt.strip():
            messages.append({
                "role": "system",
                "name": AgentConfig.IDENTITY_CONTEXT_NAME,
                "content": AgentConfig.build_identity_context(self.system_prompt),
            })

        if workspace_context.strip():
            messages.append({
                "role": "system",
                "name": AgentConfig.WORKSPACE_CONTEXT_NAME,
                "content": workspace_context.strip(),
            })

        if skills_catalog.strip():
            messages.append({
                "role": "system",
                "name": AgentConfig.SKILLS_CATALOG_NAME,
                "content": skills_catalog.strip(),
            })

        return messages

    @classmethod
    def build_tool_policy(cls, tool_names: Optional[List[str]] = None) -> str:
        ordered_names = cls.ordered_tool_policy_names(tool_names or [])
        sections = [
            AgentConfig.TOOL_SYSTEM_PROMPTS[name].strip()
            for name in ordered_names
            if name in AgentConfig.TOOL_SYSTEM_PROMPTS
        ]
        if not sections:
            return ""
        return (
            "All available tools are defined in this policy. "
            "Do not assume, invent, or reference any tools outside this list.\n\n"
            "<tool_policy>\n"
            + "\n\n".join(sections)
            + "\n\n</tool_policy>"
        )

    @staticmethod
    def ordered_tool_policy_names(tool_names: List[str]) -> list[str]:
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
