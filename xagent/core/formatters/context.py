from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone as datetime_timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from ..time import format_in_timezone, resolve_timezone


@dataclass(frozen=True)
class RoomContextEntry:
    """A prompt-time transcript entry for scoped multi-participant chat."""

    speaker_label: str
    occurred_at: datetime
    text: str
    is_self: bool = False


def format_room_context(
    room_id: str,
    entries: Iterable[RoomContextEntry],
    *,
    room_name: Optional[str] = None,
    timezone: ZoneInfo | None = None,
) -> str:
    """Render a room-context block understood by the core prompt."""
    safe_room_id = sanitize_room_context_field(room_id)
    body = format_room_context_body(entries, timezone=timezone)
    if not safe_room_id or not body:
        return body

    safe_room_name = sanitize_room_context_field(room_name)
    header_lines = ["[room context]"]
    if safe_room_name:
        header_lines.append(f"room_name: {safe_room_name}")
    header_lines.append(f"room_id: {safe_room_id}")
    return "\n".join([*header_lines, "", body, "[/room context]"])


def format_room_context_body(
    entries: Iterable[RoomContextEntry],
    *,
    timezone: ZoneInfo | None = None,
) -> str:
    """Render room-context lines ordered oldest to newest."""
    lines: list[str] = []
    for entry in sorted(entries, key=_room_context_sort_key):
        line = format_room_context_entry(entry, timezone=timezone)
        if line:
            lines.append(line)
    return "\n".join(lines).strip()


def format_room_context_entry(
    entry: RoomContextEntry,
    *,
    timezone: ZoneInfo | None = None,
) -> Optional[str]:
    """Render a single structured room-context entry."""
    speaker = "you" if entry.is_self else sanitize_room_context_field(entry.speaker_label)
    text = " ".join((entry.text or "").split())
    if not speaker or not text:
        return None
    return f"{speaker} {format_room_context_timestamp(entry.occurred_at, timezone=timezone)}: {text}"


def format_room_context_timestamp(
    occurred_at: datetime,
    *,
    timezone: ZoneInfo | None = None,
) -> str:
    """Format an entry timestamp for room-context transcript lines."""
    source_time = occurred_at
    if source_time.tzinfo is None:
        source_time = source_time.replace(tzinfo=datetime_timezone.utc)
    return format_in_timezone(source_time.timestamp(), timezone or resolve_timezone())


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
