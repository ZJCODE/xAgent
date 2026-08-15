"""Ordered, named prompt sections assembled before each model step.

Registrations emit the same ``{role, name, content}`` dicts the model client
already consumes. Empty render results are skipped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from .config import AgentConfig

KIND_INSTRUCTIONS = "instructions"
KIND_TURN = "turn"
KIND_DECISION = "decision"


@dataclass
class PromptAssembleContext:
    """Runtime values available to section renderers."""

    system_prompt: str = ""
    tool_names: Optional[List[str]] = None
    skills_catalog: str = ""
    workspace_context: str = ""
    supports_vision: bool = True
    is_subconscious: bool = False
    relationship_context: str = ""
    memory_context: str = ""
    recent_experience: str = ""
    current_user_id: str = ""
    current_time: str = ""
    channel_instructions: str = ""
    task_mode: str = "reply"
    inbox_kind: str = ""


@dataclass(frozen=True)
class PromptSection:
    name: str
    role: str
    order: int
    kind: str
    render: Callable[[PromptAssembleContext], str]


class PromptRegistry:
    """Named prompt-section registry with stable assemble order."""

    def __init__(self, sections: Optional[List[PromptSection]] = None) -> None:
        self._sections: Dict[Tuple[str, str], PromptSection] = {}
        for section in sections or []:
            self.section(section)

    def section(self, section: PromptSection) -> Callable[[], None]:
        key = (section.kind, section.name)
        if key in self._sections:
            raise ValueError(f"duplicate prompt section {section.kind}/{section.name}")
        self._sections[key] = section

        def dispose() -> None:
            self._sections.pop(key, None)

        return dispose

    def assemble(self, kind: str, ctx: PromptAssembleContext) -> list[dict]:
        selected = [section for section in self._sections.values() if section.kind == kind]
        selected.sort(key=lambda section: (section.order, section.name))
        messages: list[dict] = []
        for section in selected:
            content = str(section.render(ctx) or "").strip()
            if not content:
                continue
            messages.append({
                "role": section.role,
                "name": section.name,
                "content": content,
            })
        return messages


def _render_core_interaction_rules(ctx: PromptAssembleContext) -> str:
    core_prompt = AgentConfig.BASE_AGENT_PROMPT.strip()
    if not ctx.supports_vision:
        core_prompt += AgentConfig.NO_VISION_NOTICE.rstrip()
    if ctx.is_subconscious:
        core_prompt += AgentConfig.SUBCONSCIOUS_MODE_NOTICE.rstrip()
    return core_prompt


def _render_tool_policy(ctx: PromptAssembleContext) -> str:
    if not ctx.tool_names:
        return ""
    return AgentConfig.TOOL_POLICY_BASELINE


def _render_identity(ctx: PromptAssembleContext) -> str:
    identity = (ctx.system_prompt or "").strip()
    if not identity:
        return ""
    return AgentConfig.build_identity_context(identity)


def _render_workspace(ctx: PromptAssembleContext) -> str:
    return (ctx.workspace_context or "").strip()


def _render_skills(ctx: PromptAssembleContext) -> str:
    return (ctx.skills_catalog or "").strip()


def _render_relationship(ctx: PromptAssembleContext) -> str:
    relationships = (ctx.relationship_context or "").strip()
    if not relationships or ctx.task_mode == "subconscious_json":
        return ""
    return AgentConfig.build_relationship_context(relationships)


def _render_subconscious_relationships(ctx: PromptAssembleContext) -> str:
    relationships = (ctx.relationship_context or "").strip()
    if not relationships or ctx.task_mode != "subconscious_json":
        return ""
    return AgentConfig.build_subconscious_relationships_context(relationships)


def _render_memory(ctx: PromptAssembleContext) -> str:
    memory = (ctx.memory_context or "").strip()
    if not memory:
        return ""
    return (
        f"<{AgentConfig.RECENT_MEMORY_NAME}>\n\n"
        f"{memory}\n\n"
        f"</{AgentConfig.RECENT_MEMORY_NAME}>"
    )


def _render_experience(ctx: PromptAssembleContext) -> str:
    return (ctx.recent_experience or "").strip()


def _render_current_task(ctx: PromptAssembleContext) -> str:
    resolved_current_time = ctx.current_time or datetime.now().strftime("%Y-%m-%d %H:%M")
    if ctx.task_mode == "subconscious_json":
        return AgentConfig.build_subconscious_current_task(current_time=resolved_current_time)
    return AgentConfig.build_current_task(
        current_user_id=ctx.current_user_id,
        current_time=resolved_current_time,
        inbox_kind=ctx.inbox_kind,
    )


def _render_channel_instructions(ctx: PromptAssembleContext) -> str:
    text = (ctx.channel_instructions or "").strip()
    if not text or ctx.task_mode == "subconscious_json":
        return ""
    return (
        f"<{AgentConfig.CHANNEL_INSTRUCTIONS_NAME}>\n"
        f"{text}\n"
        f"</{AgentConfig.CHANNEL_INSTRUCTIONS_NAME}>"
    )


def _render_decision_rules(_ctx: PromptAssembleContext) -> str:
    return AgentConfig.DECISION_SYSTEM_PROMPT


def default_prompt_registry() -> PromptRegistry:
    """Builtin sections matching the historical MessageHandler layer order."""
    registry = PromptRegistry()
    for section in (
        PromptSection(
            name=AgentConfig.CORE_INTERACTION_RULES_NAME,
            role="system",
            order=-100,
            kind=KIND_INSTRUCTIONS,
            render=_render_core_interaction_rules,
        ),
        PromptSection(
            name=AgentConfig.TOOL_POLICY_NAME,
            role="system",
            order=-50,
            kind=KIND_INSTRUCTIONS,
            render=_render_tool_policy,
        ),
        PromptSection(
            name=AgentConfig.IDENTITY_CONTEXT_NAME,
            role="system",
            order=0,
            kind=KIND_INSTRUCTIONS,
            render=_render_identity,
        ),
        PromptSection(
            name=AgentConfig.WORKSPACE_CONTEXT_NAME,
            role="system",
            order=10,
            kind=KIND_INSTRUCTIONS,
            render=_render_workspace,
        ),
        PromptSection(
            name=AgentConfig.SKILLS_CATALOG_NAME,
            role="system",
            order=20,
            kind=KIND_INSTRUCTIONS,
            render=_render_skills,
        ),
        PromptSection(
            name=AgentConfig.RELATIONSHIP_CONTEXT_NAME,
            role="user",
            order=0,
            kind=KIND_TURN,
            render=_render_relationship,
        ),
        PromptSection(
            name=AgentConfig.SUBCONSCIOUS_RELATIONSHIPS_NAME,
            role="user",
            order=0,
            kind=KIND_TURN,
            render=_render_subconscious_relationships,
        ),
        PromptSection(
            name=AgentConfig.RECENT_MEMORY_NAME,
            role="user",
            order=10,
            kind=KIND_TURN,
            render=_render_memory,
        ),
        PromptSection(
            name=AgentConfig.RECENT_EXPERIENCE_NAME,
            role="user",
            order=20,
            kind=KIND_TURN,
            render=_render_experience,
        ),
        PromptSection(
            name=AgentConfig.CURRENT_TASK_NAME,
            role="user",
            order=30,
            kind=KIND_TURN,
            render=_render_current_task,
        ),
        PromptSection(
            name=AgentConfig.CHANNEL_INSTRUCTIONS_NAME,
            role="user",
            order=40,
            kind=KIND_TURN,
            render=_render_channel_instructions,
        ),
        PromptSection(
            name=AgentConfig.DECISION_RULES_NAME,
            role="system",
            order=-100,
            kind=KIND_DECISION,
            render=_render_decision_rules,
        ),
        PromptSection(
            name=AgentConfig.IDENTITY_CONTEXT_NAME,
            role="system",
            order=0,
            kind=KIND_DECISION,
            render=_render_identity,
        ),
    ):
        registry.section(section)
    return registry
