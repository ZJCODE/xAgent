"""Scene YAML loading and relative schedule parsing."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

from .models import Room
from .paths import validate_room_id, validate_world_id

_DURATION_RE = re.compile(
    r"^\+(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+)s)?$"
)


@dataclass(frozen=True)
class ScheduledScene:
    room_id: str
    text: str
    delay_seconds: float
    at_raw: str

    def fire_key(self) -> str:
        digest = hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:12]
        return f"{self.room_id}:{self.at_raw}:{digest}"


def scene_fire_key(scheduled: ScheduledScene) -> str:
    return scheduled.fire_key()


@dataclass(frozen=True)
class SceneConfig:
    world_id: str
    rooms: tuple[Room, ...]
    scenes: tuple[ScheduledScene, ...] = ()

    def room(self, room_id: str) -> Optional[Room]:
        for room in self.rooms:
            if room.id == room_id:
                return room
        return None


def parse_relative_delay(value: str) -> float:
    """Parse ``+5m``, ``+1h30m``, ``+10s`` into seconds."""
    raw = str(value or "").strip()
    match = _DURATION_RE.match(raw)
    if not match or raw == "+":
        raise ValueError(f"invalid scene schedule: {value!r} (expected +5m, +1h, +30s, ...)")
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    total = hours * 3600 + minutes * 60 + seconds
    if total <= 0:
        raise ValueError(f"scene delay must be positive: {value!r}")
    return float(total)


def load_scene_file(path: Path | str) -> SceneConfig:
    scene_path = Path(path).expanduser().resolve()
    data = yaml.safe_load(scene_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"scene file must be a mapping: {scene_path}")
    return parse_scene_config(data)


def parse_scene_config(data: dict[str, Any]) -> SceneConfig:
    world_id = validate_world_id(str(data.get("world") or "").strip())

    rooms_raw = data.get("rooms") or []
    if not isinstance(rooms_raw, list) or not rooms_raw:
        raise ValueError("scene.rooms must be a non-empty list")

    rooms: list[Room] = []
    seen: set[str] = set()
    for item in rooms_raw:
        if not isinstance(item, dict):
            raise ValueError("each room must be a mapping")
        room_id = validate_room_id(str(item.get("id") or "").strip())
        name = str(item.get("name") or room_id).strip()
        setting = str(item.get("setting") or "").strip()
        if room_id in seen:
            raise ValueError(f"duplicate room.id: {room_id}")
        seen.add(room_id)
        rooms.append(Room(id=room_id, name=name, setting=setting))

    scenes_raw = data.get("scenes") or []
    if scenes_raw is None:
        scenes_raw = []
    if not isinstance(scenes_raw, list):
        raise ValueError("scene.scenes must be a list")

    room_ids = {room.id for room in rooms}
    scenes: list[ScheduledScene] = []
    for item in scenes_raw:
        if not isinstance(item, dict):
            raise ValueError("each scene entry must be a mapping")
        at_raw = str(item.get("at") or "").strip()
        room_id = str(item.get("room") or "").strip()
        if room_id:
            room_id = validate_room_id(room_id)
        text = str(item.get("text") or "").strip()
        if not at_raw or not room_id or not text:
            raise ValueError("scene entries require at, room, and text")
        if room_id not in room_ids:
            raise ValueError(f"scene room unknown: {room_id}")
        scenes.append(
            ScheduledScene(
                room_id=room_id,
                text=text,
                delay_seconds=parse_relative_delay(at_raw),
                at_raw=at_raw,
            )
        )

    return SceneConfig(world_id=world_id, rooms=tuple(rooms), scenes=tuple(scenes))


DEFAULT_PLAZA_SCENE = """\
world: plaza
rooms:
  - id: hall
    name: 大厅
    setting: 傍晚的开放大厅，谁都可以进来说话
scenes:
  - at: "+5m"
    room: hall
    text: 天色暗下来了
"""
