from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from ..config import AgentConfig
from ..context_text import cap_message_content


@dataclass(frozen=True)
class RoomContextEntry:
    """A prompt-time transcript entry for scoped multi-participant chat."""

    speaker_label: str
    occurred_at: datetime
    text: str
    is_self: bool = False
    event_id: Optional[str] = None
    speaker_key: Optional[str] = None


@dataclass(frozen=True)
class RoomSnapshot:
    """Structured room situation for dedupe and rendering."""

    room_id: str
    room_name: str
    entries: tuple[RoomContextEntry, ...]
    present_labels: tuple[str, ...] = ()
    present_keys: tuple[str, ...] = ()

    @property
    def event_keys(self) -> set[str]:
        keys: set[str] = set()
        for entry in self.entries:
            if entry.event_id:
                keys.add(entry.event_id)
        return keys

    def render(self) -> str:
        return format_room_context(
            self.room_id,
            self.entries,
            room_name=self.room_name or None,
            present=self.present_labels or None,
        )

    @classmethod
    def from_legacy_text(cls, text: str) -> "RoomSnapshot":
        """Parse a rendered ``[room context]`` block without entry event ids."""
        raw = (text or "").strip()
        room_id = ""
        room_name = ""
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.startswith("room_id:"):
                room_id = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("room_name:"):
                room_name = stripped.split(":", 1)[1].strip()
        return cls(
            room_id=room_id,
            room_name=room_name,
            entries=(),
            present_labels=(),
            present_keys=(),
        )


def format_room_context(
    room_id: str,
    entries: Iterable[RoomContextEntry],
    *,
    room_name: Optional[str] = None,
    present: Optional[Iterable[str]] = None,
) -> str:
    """Render a room-context block understood by the core prompt."""
    safe_room_id = sanitize_room_context_field(room_id)
    body = format_room_context_body(entries)
    if not safe_room_id or not body:
        return body

    safe_room_name = sanitize_room_context_field(room_name)
    header_lines = ["[room context]"]
    if safe_room_name and safe_room_name != safe_room_id:
        header_lines.append(f"room_name: {safe_room_name}")
    header_lines.append(f"room_id: {safe_room_id}")
    present_line = format_room_context_present(present)
    if present_line:
        header_lines.append(present_line)
    return "\n".join([*header_lines, "", body, "[/room context]"])


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
    text = cap_message_content(
        " ".join((entry.text or "").split()),
        AgentConfig.MAX_ROOM_ENTRY_CHARS,
    )
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
