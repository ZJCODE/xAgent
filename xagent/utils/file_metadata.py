"""Shared file descriptions for workspace and skill browsers."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from .text_file import is_binary_file


def file_metadata(path: Path, *, root: Path) -> dict[str, Any]:
    """Describe an already-validated path relative to its storage root."""
    resolved = path.resolve()
    stat = resolved.stat()
    is_dir = resolved.is_dir()
    mime_type, _ = mimetypes.guess_type(resolved.name)
    return {
        "name": resolved.name,
        "path": str(resolved.relative_to(root)),
        "type": "dir" if is_dir else "file",
        "size": stat.st_size,
        "modified": stat.st_mtime,
        "mime_type": mime_type or "application/octet-stream",
        "binary": False if is_dir else is_binary_file(resolved),
    }
