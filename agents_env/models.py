"""Protocol and domain objects for the agents environment."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class EventKind(str, Enum):
    UTTERANCE = "utterance"
    JOIN = "join"
    LEAVE = "leave"
    SCENE = "scene"

    @classmethod
    def parse(cls, value: str) -> "EventKind | str":
        """Parse a kind; unknown values pass through so old clients survive new logs."""
        try:
            return cls(value)
        except ValueError:
            return str(value or "")


@dataclass(frozen=True)
class Room:
    id: str
    name: str
    setting: str = ""


@dataclass(frozen=True)
class Member:
    id: str
    display_name: str


@dataclass(frozen=True)
class WorldEvent:
    seq: int
    ts: float
    room_id: str
    kind: EventKind | str
    actor_id: str
    text: str
    mentions: tuple[str, ...] = ()
    room_seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        kind = self.kind.value if isinstance(self.kind, EventKind) else str(self.kind)
        return {
            "seq": self.seq,
            "room_seq": self.room_seq,
            "ts": self.ts,
            "room_id": self.room_id,
            "kind": kind,
            "actor_id": self.actor_id,
            "text": self.text,
            "mentions": list(self.mentions),
        }

    @classmethod
    def from_row(
        cls,
        *,
        seq: int,
        ts: float,
        room_id: str,
        kind: str,
        actor_id: str,
        text: str,
        mentions_json: str,
        room_seq: int = 0,
        **_extra: Any,
    ) -> "WorldEvent":
        mentions_raw = json.loads(mentions_json or "[]")
        mentions = tuple(str(item) for item in mentions_raw)
        return cls(
            seq=seq,
            ts=ts,
            room_id=room_id,
            kind=EventKind.parse(kind),
            actor_id=actor_id,
            text=text,
            mentions=mentions,
            room_seq=int(room_seq or 0),
        )


@dataclass
class PresenceRecord:
    member_id: str
    room_id: str
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


def room_to_dict(room: Room) -> dict[str, Any]:
    return asdict(room)


def member_to_dict(member: Member) -> dict[str, Any]:
    return asdict(member)
