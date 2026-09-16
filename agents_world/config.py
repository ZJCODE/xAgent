"""World identity: one place. Not a script, not a mind."""

from __future__ import annotations

from dataclasses import dataclass

from .paths import validate_world_id


@dataclass(frozen=True)
class WorldConfig:
    world_id: str
    name: str

    @classmethod
    def create(cls, *, world_id: str, name: str = "") -> "WorldConfig":
        wid = validate_world_id(world_id)
        label = str(name or "").strip() or wid
        return cls(world_id=wid, name=label)
