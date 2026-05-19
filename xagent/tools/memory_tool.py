"""High-level long-term memory tools."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from xagent.components.memory import MemoryKind, SubjectType
from xagent.utils.tool_decorator import function_tool

if TYPE_CHECKING:
    from xagent.components.memory import ExperienceMemoryStore


MEMORY_KIND_DESCRIPTION = (
    "One of: episodic, semantic_fact, preference, commitment, "
    "project_state, person_fact, procedure, summary."
)
SUBJECT_TYPE_DESCRIPTION = "One of: self, person, project, topic, room, system."


def create_remember_tool(memory: "ExperienceMemoryStore", is_enabled):
    """Create a high-level tool for writing durable memory."""

    @function_tool(
        name="remember",
        description=(
            "Record exactly one durable, reusable long-term fact. Use only when "
            "the user asks you to remember something or when a stable preference, "
            "commitment, project state, person fact, or procedure is explicit."
        ),
        param_descriptions={
            "content": "Concise factual memory content with clear attribution. Do not store one-off chatter.",
            "kind": f"Choose the narrowest durable kind. {MEMORY_KIND_DESCRIPTION}",
            "subject_type": SUBJECT_TYPE_DESCRIPTION,
            "subject_key": "Stable subject identifier, such as a person id/name, project name, topic, room id, or self.",
            "salience": "Importance from 0.0 to 1.0. Default 0.7.",
            "confidence": "Confidence from 0.0 to 1.0. Default 0.85.",
            "valid_until": "Optional expiry as Unix timestamp or ISO date/time.",
            "evidence_note": "Short quote or exact source phrase grounding this memory when available.",
            "sensitivity": "normal, private, sensitive, or secret. Default normal.",
        },
    )
    async def remember(
        content: str,
        kind: str = MemoryKind.SEMANTIC_FACT,
        subject_type: str = SubjectType.SELF,
        subject_key: str = "self",
        salience: float = 0.7,
        confidence: float = 0.85,
        valid_until: Optional[str] = None,
        evidence_note: Optional[str] = None,
        sensitivity: str = "normal",
    ) -> dict:
        if not is_enabled():
            return {"status": "disabled", "message": "Memory writing is disabled for this turn."}
        content = str(content or "").strip()
        if not content:
            return {"status": "skipped", "message": "Empty memory content."}
        memory_id = await memory.remember(
            content=content,
            kind=kind,
            subject_type=subject_type,
            subject_key=subject_key,
            salience=salience,
            confidence=confidence,
            valid_until=valid_until,
            evidence_note=evidence_note,
            sensitivity=sensitivity,
            metadata={"source": "tool"},
        )
        return {"status": "ok", "memory_id": memory_id}

    return remember


def create_recall_memory_tool(memory: "ExperienceMemoryStore", is_enabled):
    """Create a high-level ordinary recall tool."""

    @function_tool(
        name="recall_memory",
        description=(
            "Recall durable memory and summaries. Use for prior preferences, "
            "commitments, project state, reusable facts, procedures, and person "
            "facts when the prompt brief is not enough."
        ),
        param_descriptions={
            "query": "Natural language recall cue for durable memory, not raw transcript search.",
            "subject_type": f"Optional subject filter. {SUBJECT_TYPE_DESCRIPTION}",
            "subject_key": "Optional stable subject identifier.",
            "time_range": "Optional time range: 'YYYY-MM-DD to YYYY-MM-DD', Unix range, or empty.",
            "kinds": f"Optional comma-separated kind filter. {MEMORY_KIND_DESCRIPTION}",
            "include_evidence": "Whether to include evidence quote/event ids. Default false.",
            "max_items": "Maximum memory items to return. Default 8.",
        },
    )
    async def recall_memory(
        query: str,
        subject_type: Optional[str] = None,
        subject_key: Optional[str] = None,
        time_range: Optional[str] = None,
        kinds: Optional[str] = None,
        include_evidence: bool = False,
        max_items: int = 8,
    ) -> dict:
        if not is_enabled():
            return {"status": "disabled", "enabled": False, "message": "Memory reading is disabled for this turn."}
        result = await memory.recall_memory(
            query=query,
            subject_type=subject_type,
            subject_key=subject_key,
            time_range=time_range,
            kinds=kinds,
            include_evidence=include_evidence,
            max_items=max_items,
        )
        result["enabled"] = True
        return result

    return recall_memory


def create_search_history_tool(memory: "ExperienceMemoryStore", is_enabled):
    """Create a deep raw event search tool."""

    @function_tool(
        name="search_history",
        description=(
            "Search raw event history for exact older conversation details. Use "
            "after ordinary recall is insufficient, or when the user asks for exact "
            "wording, audit trails, or old chat review."
        ),
        param_descriptions={
            "query": "Natural language or keyword text to match against raw events.",
            "time_range": "Optional time range: 'YYYY-MM-DD to YYYY-MM-DD', Unix range, or empty.",
            "conversation_id": "Optional conversation id filter.",
            "speaker_id": "Optional speaker id filter.",
            "max_events": "Maximum raw events to return. Default 20.",
        },
    )
    async def search_history(
        query: str,
        time_range: Optional[str] = None,
        conversation_id: Optional[str] = None,
        speaker_id: Optional[str] = None,
        max_events: int = 20,
    ) -> dict:
        if not is_enabled():
            return {"status": "disabled", "enabled": False, "message": "Memory reading is disabled for this turn."}
        result = await memory.search_history(
            query=query,
            time_range=time_range,
            conversation_id=conversation_id,
            speaker_id=speaker_id,
            max_events=max_events,
        )
        result["enabled"] = True
        return result

    return search_history


def create_correct_memory_tool(memory: "ExperienceMemoryStore", is_enabled):
    """Create a correction tool that preserves revision history."""

    @function_tool(
        name="correct_memory",
        description=(
            "Correct an existing memory when the user says it is wrong, outdated, "
            "or gives a clearer replacement. The store preserves revision history."
        ),
        param_descriptions={
            "correction": "Correct replacement memory content.",
            "reason": "Why this correction is being made.",
            "memory_id": "Optional exact memory id.",
            "query": "Optional lookup cue when memory_id is unknown.",
        },
    )
    async def correct_memory(
        correction: str,
        reason: str,
        memory_id: Optional[int] = None,
        query: Optional[str] = None,
    ) -> dict:
        if not is_enabled():
            return {"status": "disabled", "message": "Memory correction is disabled for this turn."}
        return await memory.correct_memory(
            memory_id=memory_id,
            query=query,
            correction=correction,
            reason=reason,
            actor="tool",
        )

    return correct_memory


def create_forget_memory_tool(memory: "ExperienceMemoryStore", is_enabled):
    """Create a forget/delete memory tool."""

    @function_tool(
        name="forget_memory",
        description=(
            "Archive or delete an existing memory when the user asks you to forget, "
            "remove, hide, or delete it. Archive by default unless deletion is explicit."
        ),
        param_descriptions={
            "memory_id": "Optional exact memory id.",
            "query": "Optional lookup cue when memory_id is unknown.",
            "mode": "archive or delete. Default archive.",
            "reason": "Why this memory should be forgotten.",
        },
    )
    async def forget_memory(
        memory_id: Optional[int] = None,
        query: Optional[str] = None,
        mode: str = "archive",
        reason: str = "forget requested",
    ) -> dict:
        if not is_enabled():
            return {"status": "disabled", "message": "Memory forgetting is disabled for this turn."}
        return await memory.forget_memory(
            memory_id=memory_id,
            query=query,
            mode=mode,
            reason=reason,
            actor="tool",
        )

    return forget_memory
