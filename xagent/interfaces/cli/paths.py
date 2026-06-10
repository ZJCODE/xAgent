"""Shared runtime path helpers for the CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..base import BaseAgentConfig
from .channels import load_config_file


def runtime_dir(args: Any) -> Path:
    raw_dir = getattr(args, "config_dir", None) or BaseAgentConfig.DEFAULT_CONFIG_DIR
    return Path(raw_dir).expanduser().resolve()


def config_path(args: Any) -> Path:
    return runtime_dir(args) / BaseAgentConfig.CONFIG_FILENAME


def identity_path(args: Any) -> Path:
    return runtime_dir(args) / BaseAgentConfig.IDENTITY_FILENAME


def load_runtime_config(args: Any) -> dict[str, Any]:
    return load_config_file(runtime_dir(args))