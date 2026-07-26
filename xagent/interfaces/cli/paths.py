"""Shared runtime path helpers for the CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...core.agent_factory import AgentPaths
from .agents import ensure_default_agent_for_setup, resolve_agent_runtime_dir


def runtime_dir(args: Any) -> Path:
    raw_dir = getattr(args, "config_dir", None)
    if raw_dir:
        return Path(raw_dir).expanduser().resolve()
    return resolve_agent_runtime_dir(getattr(args, "agent", None))


def setup_runtime_dir(args: Any) -> Path:
    raw_dir = getattr(args, "config_dir", None)
    if raw_dir:
        return Path(raw_dir).expanduser().resolve()
    return ensure_default_agent_for_setup(getattr(args, "agent", None))


def config_path(args: Any) -> Path:
    return runtime_dir(args) / AgentPaths.CONFIG_FILENAME


def identity_path(args: Any) -> Path:
    return runtime_dir(args) / AgentPaths.IDENTITY_FILENAME
