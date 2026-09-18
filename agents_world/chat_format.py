"""Human-readable formatting for terminal world chat."""

from __future__ import annotations

from typing import Any, Mapping, Optional


def actor_label(actor_id: str, member_id: str) -> str:
    return "you" if actor_id == member_id else actor_id


def format_chat_event(event: Mapping[str, Any], *, member_id: str) -> Optional[str]:
    """Return a single line for an event, or None to skip."""
    kind = str(event.get("kind") or "")
    actor = str(event.get("actor_id") or "")
    text = event.get("text")
    if kind == "utterance":
        body = str(text or "").strip()
        attachments = event.get("attachments") or []
        if not body and attachments:
            who = actor_label(actor, member_id)
            return f"{who}: (shared a file)"
        if not body:
            return None
        return f"{actor_label(actor, member_id)}: {body}"
    if kind == "join":
        return f"· {actor_label(actor, member_id)} joined"
    if kind == "leave":
        return f"· {actor_label(actor, member_id)} left"
    suffix = f": {text}" if text else ""
    return f"· [{kind}] {actor_label(actor, member_id)}{suffix}"


def format_present_roster(present: list[Mapping[str, Any]], *, member_id: str) -> str:
    if not present:
        return "(no one else here yet)"
    parts: list[str] = []
    for person in present:
        mid = str(person.get("member_id") or "")
        name = str(person.get("display_name") or mid)
        if mid == member_id:
            parts.append("you")
        elif name != mid:
            parts.append(f"{name} ({mid})")
        else:
            parts.append(mid)
    return ", ".join(parts)


def format_join_intro(
    *,
    world_name: str,
    present: list[Mapping[str, Any]],
    member_id: str,
    event_count: int,
    full_history: bool,
) -> list[str]:
    title = world_name or "world"
    lines = [
        f"In {title}. Here now: {format_present_roster(present, member_id=member_id)}",
    ]
    if event_count and not full_history:
        lines.append(
            f"({event_count} earlier message{'s' if event_count != 1 else ''} in the log; "
            "use --full-history to print them)",
        )
    lines.append("Type a message and press Enter. /leave to leave, /quit to exit.")
    return lines
