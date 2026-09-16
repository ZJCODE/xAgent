"""World-owned file blobs. These belong to the venue log, not an agent workspace."""

from __future__ import annotations

import uuid
from pathlib import Path

from . import MAX_FILE_NAME_LENGTH


def new_file_id() -> str:
    return uuid.uuid4().hex


def sanitize_file_name(name: str) -> str:
    raw = Path(str(name or "")).name.strip().replace("\x00", "")
    if not raw or raw in {".", ".."}:
        return "file"
    return raw[:MAX_FILE_NAME_LENGTH]


def normalize_mime(mime: str) -> str:
    raw = str(mime or "").strip().split(";", 1)[0].strip().lower()
    if not raw or "/" not in raw or len(raw) > 128:
        return "application/octet-stream"
    kind, _, subtype = raw.partition("/")
    if not kind or not subtype or " " in raw:
        return "application/octet-stream"
    return raw
