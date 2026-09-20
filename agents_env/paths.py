"""Filesystem paths for agents_env data (independent of ~/.xagent/agents)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from . import DEFAULT_DATA_ROOT, MAX_ID_LENGTH

# Must start with an alphanumeric so '.' / '..' cannot be a world id.
_ID_RE = re.compile(rf"^[A-Za-z0-9][A-Za-z0-9._-]{{0,{MAX_ID_LENGTH - 1}}}$")


def validate_id(value: str, *, label: str = "id") -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{label} is required")
    if ".." in raw or not _ID_RE.fullmatch(raw):
        raise ValueError(
            f"{label} must be 1-{MAX_ID_LENGTH} chars of [A-Za-z0-9._-] "
            f"and must start with alphanumeric: {value!r}"
        )
    return raw


def validate_world_id(world_id: str) -> str:
    return validate_id(world_id, label="world_id")


def validate_room_id(room_id: str) -> str:
    return validate_id(room_id, label="room_id")


def validate_member_id(member_id: str) -> str:
    return validate_id(member_id, label="member_id")


def resolve_data_root(root: Optional[Path | str] = None) -> Path:
    if root is None:
        return Path(DEFAULT_DATA_ROOT).expanduser().resolve()
    return Path(root).expanduser().resolve()


def world_data_dir(world_id: str, *, root: Optional[Path | str] = None) -> Path:
    world_id = validate_world_id(world_id)
    base = resolve_data_root(root)
    path = (base / "worlds" / world_id).resolve()
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"world data dir escaped data root: {path}") from exc
    return path
