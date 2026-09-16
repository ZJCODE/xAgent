"""Protocol and domain objects for the world."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional
from urllib.parse import quote


class EventKind(str, Enum):
    UTTERANCE = "utterance"
    JOIN = "join"
    LEAVE = "leave"

    @classmethod
    def parse(cls, value: str) -> "EventKind | str":
        """Parse a kind; unknown values pass through."""
        try:
            return cls(value)
        except ValueError:
            return str(value or "")


@dataclass(frozen=True)
class Member:
    id: str
    display_name: str


@dataclass(frozen=True)
class Attachment:
    id: str
    name: str
    mime: str
    size: int

    def to_dict(self, *, world_id: str) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "mime": self.mime,
            "size": self.size,
            "url": f"/worlds/{quote(world_id, safe='')}/files/{quote(self.id, safe='')}",
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Optional["Attachment"]:
        if not isinstance(raw, dict):
            return None
        file_id = str(raw.get("id") or "").strip()
        name = str(raw.get("name") or "").strip() or "file"
        mime = str(raw.get("mime") or "").strip() or "application/octet-stream"
        try:
            size = int(raw.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if not file_id:
            return None
        return cls(id=file_id, name=name, mime=mime, size=max(size, 0))


@dataclass(frozen=True)
class WorldEvent:
    seq: int
    ts: float
    kind: EventKind | str
    actor_id: str
    text: str
    mentions: tuple[str, ...] = ()
    attachments: tuple[Attachment, ...] = ()

    def to_dict(self, *, world_id: str) -> dict[str, Any]:
        kind = self.kind.value if isinstance(self.kind, EventKind) else str(self.kind)
        body: dict[str, Any] = {
            "seq": self.seq,
            "ts": self.ts,
            "kind": kind,
            "actor_id": self.actor_id,
            "text": self.text,
            "mentions": list(self.mentions),
        }
        if self.attachments:
            body["attachments"] = [item.to_dict(world_id=world_id) for item in self.attachments]
        return body

    @classmethod
    def from_row(
        cls,
        *,
        seq: int,
        ts: float,
        kind: str,
        actor_id: str,
        text: str,
        mentions_json: str,
        attachments_json: str = "[]",
        **_extra: Any,
    ) -> "WorldEvent":
        mentions_raw = json.loads(mentions_json or "[]")
        mentions = tuple(str(item) for item in mentions_raw)
        attachments_raw = json.loads(attachments_json or "[]")
        attachments = tuple(
            item
            for item in (Attachment.from_dict(raw) for raw in attachments_raw)
            if item is not None
        )
        return cls(
            seq=seq,
            ts=ts,
            kind=EventKind.parse(kind),
            actor_id=actor_id,
            text=text,
            mentions=mentions,
            attachments=attachments,
        )


@dataclass
class PresenceRecord:
    member_id: str
    display_name: str
    present: bool = True


@dataclass
class ClientMessage:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, raw: str) -> "ClientMessage":
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("message must be a JSON object")
        msg_type = str(data.get("type") or "").strip()
        if not msg_type:
            raise ValueError("message.type is required")
        payload = {k: v for k, v in data.items() if k != "type"}
        return cls(type=msg_type, payload=payload)


def encode_server_message(msg_type: str, **payload: Any) -> str:
    body = {"type": msg_type, **payload}
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def encode_error(code: str, message: str, **extra: Any) -> str:
    return encode_server_message("error", code=code, message=message, **extra)


def member_to_dict(member: Member) -> dict[str, Any]:
    return asdict(member)
