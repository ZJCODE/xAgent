from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional


@dataclass(frozen=True)
class RoomContextEntry:
    """A prompt-time transcript entry for scoped multi-participant chat."""

    speaker_label: str
    occurred_at: datetime
    text: str
    is_self: bool = False


ROOM_CONTEXT_COVERS_PREFIX = "covers: "
ROOM_CONTEXT_COVERS_SEPARATOR = ".."
_ROOM_CONTEXT_COVERS_RE = re.compile(
    r"^covers:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\.\.(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*$",
    re.MULTILINE,
)


def format_room_context(
    room_id: str,
    entries: Iterable[RoomContextEntry],
    *,
    room_name: Optional[str] = None,
    present: Optional[Iterable[str]] = None,
) -> str:
    """Render a room-context block understood by the core prompt.

    The header carries a ``covers:`` span (oldest..newest entry, minute
    precision) so the core can drop only the stored rows this block already
    replays, instead of everything from the same place.
    """
    safe_room_id = sanitize_room_context_field(room_id)
    entries = list(entries)
    body = format_room_context_body(entries)
    if not safe_room_id or not body:
        return body

    safe_room_name = sanitize_room_context_field(room_name)
    header_lines = ["[room context]"]
    if safe_room_name:
        header_lines.append(f"room_name: {safe_room_name}")
    header_lines.append(f"room_id: {safe_room_id}")
    covers_line = format_room_context_covers(entries)
    if covers_line:
        header_lines.append(covers_line)
    present_line = format_room_context_present(present)
    if present_line:
        header_lines.append(present_line)
    return "\n".join([*header_lines, "", body, "[/room context]"])


def format_room_context_covers(entries: Iterable[RoomContextEntry]) -> Optional[str]:
    """Render the ``covers:`` header line spanning the rendered entries."""
    rendered = [entry for entry in entries if format_room_context_entry(entry)]
    if not rendered:
        return None
    oldest = min(entry.occurred_at for entry in rendered)
    newest = max(entry.occurred_at for entry in rendered)
    return (
        f"{ROOM_CONTEXT_COVERS_PREFIX}"
        f"{format_room_context_timestamp(oldest)}"
        f"{ROOM_CONTEXT_COVERS_SEPARATOR}"
        f"{format_room_context_timestamp(newest)}"
    )


def parse_room_context_covers(room_context: str) -> Optional[tuple[datetime, datetime]]:
    """Return the ``(oldest, newest)`` span declared by a room-context block."""
    match = _ROOM_CONTEXT_COVERS_RE.search(room_context or "")
    if match is None:
        return None
    try:
        start = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M")
        end = datetime.strptime(match.group(2), "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    if end < start:
        start, end = end, start
    return start, end


def format_room_context_present(present: Optional[Iterable[str]]) -> Optional[str]:
    """Render a ``present:`` header line for who is currently in the room."""
    if present is None:
        return None
    labels: list[str] = []
    for item in present:
        safe = sanitize_room_context_field(item)
        if safe and safe not in labels:
            labels.append(safe)
    if not labels:
        return None
    return f"present: {', '.join(labels)}"

def format_room_context_body(entries: Iterable[RoomContextEntry]) -> str:
    """Render room-context lines ordered oldest to newest."""
    lines: list[str] = []
    for entry in sorted(entries, key=_room_context_sort_key):
        line = format_room_context_entry(entry)
        if line:
            lines.append(line)
    return "\n".join(lines).strip()


def format_room_context_entry(entry: RoomContextEntry) -> Optional[str]:
    """Render a single structured room-context entry."""
    speaker = "ME" if entry.is_self else sanitize_room_context_field(entry.speaker_label)
    text = " ".join((entry.text or "").split())
    if not speaker or not text:
        return None
    return f"{speaker} {format_room_context_timestamp(entry.occurred_at)}: {text}"


def format_room_context_timestamp(occurred_at: datetime) -> str:
    """Format an entry timestamp for room-context transcript lines."""
    return occurred_at.strftime("%Y-%m-%d %H:%M")


def sanitize_room_context_field(value: Optional[str]) -> Optional[str]:
    """Normalize structured transcript fields embedded in prompt markers."""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized.replace("\n", " ").replace("]", "")


def _room_context_sort_key(entry: RoomContextEntry) -> float:
    return entry.occurred_at.timestamp()
