"""Filesystem paths for world data under ~/.xagent/worlds/."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import unquote

from . import DEFAULT_DATA_ROOT, MAX_ID_LENGTH

# Member / file ids: alphanumeric start, allow upper case and dots.
_MEMBER_ID_RE = re.compile(rf"^[A-Za-z0-9][A-Za-z0-9._-]{{0,{MAX_ID_LENGTH - 1}}}$")

# World name/id: same rule as agent names.
_WORLD_NAME_RE = re.compile(rf"^[a-z][a-z0-9_-]{{0,{MAX_ID_LENGTH - 1}}}$")
WORLD_NAME_RULE = (
    "Name must start with a lowercase letter and use only "
    "lowercase letters, digits, hyphens, or underscores."
)


def allocate_world_id(name: str, *, taken: Iterable[str]) -> str:
    """World id is the name (with -2, -3… if already taken)."""
    occupied = {str(item) for item in taken}
    base = validate_world_id(name)
    candidate = base
    n = 2
    while candidate in occupied:
        suffix = f"-{n}"
        trimmed = base
        if len(trimmed) + len(suffix) > MAX_ID_LENGTH:
            trimmed = trimmed[: MAX_ID_LENGTH - len(suffix)]
        candidate = f"{trimmed}{suffix}"
        n += 1
    return candidate


def validate_id(value: str, *, label: str = "id") -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{label} is required")
    if ".." in raw or not _MEMBER_ID_RE.fullmatch(raw):
        raise ValueError(
            f"{label} must be 1-{MAX_ID_LENGTH} chars of [A-Za-z0-9._-] "
            f"and must start with alphanumeric: {value!r}"
        )
    return raw


def validate_world_id(world_id: str) -> str:
    raw = unquote(str(world_id or "")).strip()
    if not raw:
        raise ValueError("world_id is required")
    if ".." in raw or "/" in raw or "\\" in raw or not _WORLD_NAME_RE.fullmatch(raw):
        raise ValueError(WORLD_NAME_RULE)
    return raw


def validate_member_id(member_id: str) -> str:
    return validate_id(member_id, label="member_id")


def resolve_data_root(root: Optional[Path | str] = None) -> Path:
    if root is None:
        return Path(DEFAULT_DATA_ROOT).expanduser().resolve()
    return Path(root).expanduser().resolve()


def worlds_dir(*, root: Optional[Path | str] = None) -> Path:
    return resolve_data_root(root) / "worlds"


def world_data_dir(world_id: str, *, root: Optional[Path | str] = None) -> Path:
    world_id = validate_world_id(world_id)
    base = resolve_data_root(root)
    path = (base / "worlds" / world_id).resolve()
    try:
        path.relative_to((base / "worlds").resolve())
    except ValueError as exc:
        raise ValueError(f"world data dir escaped worlds root: {path}") from exc
    return path


def list_world_ids(*, root: Optional[Path | str] = None) -> list[str]:
    base = worlds_dir(root=root)
    if not base.is_dir():
        return []
    ids: list[str] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        if not (child / "world.sqlite3").is_file():
            continue
        try:
            ids.append(validate_world_id(child.name))
        except ValueError:
            continue
    return ids


def remove_world_dir(world_id: str, *, root: Optional[Path | str] = None) -> Path:
    """Delete ``worlds/<world_id>/`` (sqlite + spoken files). Close the store first."""
    path = world_data_dir(world_id, root=root)
    worlds_root = worlds_dir(root=root).resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(worlds_root)
    except ValueError as exc:
        raise ValueError(f"refusing to delete path outside worlds root: {resolved}") from exc
    if resolved == worlds_root:
        raise ValueError("refusing to delete worlds root")
    if not resolved.exists():
        raise FileNotFoundError(f"unknown world: {world_id}")
    shutil.rmtree(resolved)
    return resolved
