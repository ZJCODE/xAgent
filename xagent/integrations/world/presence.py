"""Persist an agent's intended world presence across process restarts.

The world hub never reads this file. It lives under the agent's ``run/``
directory so a mind can return to the same place after its API process or
the hub comes back.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

PRESENCE_FILENAME = "world.json"


def presence_path(config_dir: Path | str) -> Path:
    return Path(config_dir).expanduser().resolve() / "run" / PRESENCE_FILENAME


def world_id_from_url(world_url: str) -> str:
    path = urlparse(str(world_url or "").strip()).path or ""
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "ws":
        return unquote(parts[1])
    return ""


def read_world_presence(config_dir: Path | str) -> Optional[dict[str, Any]]:
    path = presence_path(config_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def write_world_presence(config_dir: Path | str, payload: dict[str, Any]) -> None:
    path = presence_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = dict(payload)
    world_url = str(body.get("world_url") or "").strip()
    world_id = str(body.get("world_id") or "").strip() or world_id_from_url(world_url)
    recorded = {
        "world_url": world_url,
        "world_id": world_id,
        "member_id": str(body.get("member_id") or "").strip(),
        "display_name": str(body.get("display_name") or "").strip(),
        "want_present": bool(body.get("want_present")),
        "resume_token": str(body.get("resume_token") or "").strip(),
    }
    path.write_text(json.dumps(recorded, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def mark_world_presence(
    config_dir: Path | str,
    *,
    world_url: str,
    member_id: str,
    display_name: str = "",
    world_id: str = "",
    want_present: bool = True,
) -> dict[str, Any]:
    payload = {
        "world_url": world_url,
        "world_id": world_id or world_id_from_url(world_url),
        "member_id": member_id,
        "display_name": display_name or member_id,
        "want_present": want_present,
    }
    write_world_presence(config_dir, payload)
    return payload


def resume_token_for_world(config_dir: Path | str, *, world_url: str) -> str:
    current = read_world_presence(config_dir)
    if not current:
        return ""
    if str(current.get("world_url") or "").strip() != str(world_url or "").strip():
        return ""
    return str(current.get("resume_token") or "").strip()


def save_world_resume_token(
    config_dir: Path | str,
    *,
    world_url: str,
    resume_token: str,
) -> None:
    token = str(resume_token or "").strip()
    if not token:
        return
    current = read_world_presence(config_dir) or {}
    if str(current.get("world_url") or "").strip() != str(world_url or "").strip():
        return
    current["resume_token"] = token
    write_world_presence(config_dir, current)


def mark_world_left(config_dir: Path | str) -> Optional[dict[str, Any]]:
    current = read_world_presence(config_dir) or {}
    if not current:
        return None
    current["want_present"] = False
    write_world_presence(config_dir, current)
    return current
