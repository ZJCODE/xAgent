"""Helpers for classifying assistant chat events and stored replies.

Tool-call turns may include model text. Short status lines such as
「我去看看」 are user-facing preface. Longer scratchpad — analysis,
drafts, inner reasoning — must not be delivered, stored as history, or
fed back into the next prompt as something the agent "said".
"""

from __future__ import annotations

from typing import Any, Optional

TURN_PHASE_METADATA_KEY = "turn_phase"
TURN_PHASE_PREFACE = "preface"
TURN_PHASE_FINAL = "final"
USER_VISIBLE_PREFACE_MAX_CHARS = 120


def message_turn_phase(message: Any) -> str:
    metadata = getattr(message, "metadata", None)
    if not isinstance(metadata, dict):
        return TURN_PHASE_FINAL
    phase = str(metadata.get(TURN_PHASE_METADATA_KEY) or "").strip()
    return phase or TURN_PHASE_FINAL


def is_history_preface_message(message: Any) -> bool:
    return message_turn_phase(message) == TURN_PHASE_PREFACE


def is_user_visible_preface(text: Optional[str]) -> bool:
    """Return True when tool-call text is a brief user-facing status."""
    stripped = str(text or "").strip()
    if not stripped:
        return False
    if len(stripped) > USER_VISIBLE_PREFACE_MAX_CHARS:
        return False
    if stripped.count("\n") > 1:
        return False
    return True


def event_phase(event: Optional[dict]) -> str:
    if not isinstance(event, dict):
        return TURN_PHASE_FINAL
    phase = str(event.get("phase") or "").strip()
    return phase or TURN_PHASE_FINAL


def is_deliverable_assistant_event(event: Optional[dict]) -> bool:
    """Whether a ``message_done`` event should be sent on a user channel."""
    if not isinstance(event, dict) or event.get("type") != "message_done":
        return False
    phase = event_phase(event)
    if phase == TURN_PHASE_FINAL:
        return True
    if phase == TURN_PHASE_PREFACE:
        return is_user_visible_preface(str(event.get("content") or ""))
    return False


def is_live_streamable_event(event: Optional[dict]) -> bool:
    """Whether start/delta tokens may be shown live as the user-visible reply."""
    if not isinstance(event, dict):
        return False
    if event.get("type") not in {"message_start", "message_delta"}:
        return False
    return event_phase(event) == TURN_PHASE_FINAL
