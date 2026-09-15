"""Filesystem paths for agents_env data (independent of ~/.xagent/agents)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import DEFAULT_DATA_ROOT


def resolve_data_root(root: Optional[Path | str] = None) -> Path:
    if root is None:
        return Path(DEFAULT_DATA_ROOT).expanduser().resolve()
    return Path(root).expanduser().resolve()
