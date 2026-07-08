"""Shared runtime path helpers for the CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..base import BaseAgentConfig
from .agents import AgentRegistryError, ensure_default_agent_for_setup, management_root, resolve_agent_runtime_dir
from .channels import load_config_file


def runtime_dir(args: Any) -> Path:
    raw_dir = getattr(args, "config_dir", None)
    if raw_dir:
        return Path(raw_dir).expanduser().resolve()
    return resolve_agent_runtime_dir(getattr(args, "agent", None))


def runtime_dir_or_management_root(args: Any) -> Path:
    """Resolve an agent runtime dir, falling back to the management root when empty."""
    raw_dir = getattr(args, "config_dir", None)
    if raw_dir:
        return Path(raw_dir).expanduser().resolve()
    try:
        return resolve_agent_runtime_dir(getattr(args, "agent", None))
    except AgentRegistryError:
        return management_root()


def load_runtime_config(args: Any) -> dict[str, Any]:
    return load_config_file(runtime_dir(args))


def load_client_runtime_config(args: Any) -> dict[str, Any]:
    """Load config for web/desktop clients even when no agents are registered."""
    return load_config_file(runtime_dir_or_management_root(args))


def setup_runtime_dir(args: Any) -> Path:
    raw_dir = getattr(args, "config_dir", None)
    if raw_dir:
        return Path(raw_dir).expanduser().resolve()
    return ensure_default_agent_for_setup(getattr(args, "agent", None))


def config_path(args: Any) -> Path:
    return runtime_dir(args) / BaseAgentConfig.CONFIG_FILENAME


def identity_path(args: Any) -> Path:
    return runtime_dir(args) / BaseAgentConfig.IDENTITY_FILENAME
