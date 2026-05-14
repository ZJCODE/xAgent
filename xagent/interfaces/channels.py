"""Channel names and normalization for the xAgent CLI."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

import yaml


CHANNEL_API = "api"
CHANNEL_FEISHU = "feishu"
CHANNEL_ALL = "all"
VALID_CHANNELS = {CHANNEL_API, CHANNEL_FEISHU}


class ChannelSelectionError(ValueError):
    """Raised when a user provided an invalid channel selection."""


def load_config_file(config_dir: Path) -> dict[str, Any]:
    """Load config.yaml if present; return an empty dict when absent."""
    path = config_dir / "config.yaml"
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ChannelSelectionError(f"Configuration must be a mapping: {path}")
    return data


def enabled_channels_from_config(config: Optional[Mapping[str, Any]]) -> list[str]:
    """Return public channels enabled by config for the `all` selector."""
    channels = config.get("channels") if isinstance(config, Mapping) else None
    channels = channels if isinstance(channels, Mapping) else {}

    api_cfg = channels.get(CHANNEL_API)
    api_cfg = api_cfg if isinstance(api_cfg, Mapping) else {}
    api_enabled = bool(api_cfg.get("enabled", True))

    result: list[str] = []
    if api_enabled:
        result.append(CHANNEL_API)

    feishu_cfg = channels.get(CHANNEL_FEISHU)
    feishu_cfg = feishu_cfg if isinstance(feishu_cfg, Mapping) else {}
    if bool(feishu_cfg.get("enabled", False)):
        result.append(CHANNEL_FEISHU)

    return result or [CHANNEL_API]


def normalize_channel_values(
    values: Optional[Sequence[str]],
    *,
    default: str,
    config: Optional[Mapping[str, Any]] = None,
) -> list[str]:
    """Normalize comma-separated/repeated channel values into public channels."""
    raw_values: Iterable[str] = values if values else (default,)
    selected: list[str] = []
    for raw_value in raw_values:
        for token in str(raw_value).split(","):
            channel = token.strip().lower()
            if not channel:
                continue
            if channel == CHANNEL_ALL:
                for enabled in enabled_channels_from_config(config):
                    if enabled not in selected:
                        selected.append(enabled)
                continue
            if channel not in VALID_CHANNELS:
                valid = ", ".join(sorted(VALID_CHANNELS | {CHANNEL_ALL}))
                raise ChannelSelectionError(f"Unknown channel {channel!r}. Expected one of: {valid}.")
            if channel not in selected:
                selected.append(channel)

    return selected or [CHANNEL_API]


def api_config(config: Mapping[str, Any]) -> dict[str, Any]:
    channels = config.get("channels") if isinstance(config, Mapping) else None
    if not isinstance(channels, Mapping):
        return {}
    data = channels.get(CHANNEL_API)
    return dict(data) if isinstance(data, Mapping) else {}


def feishu_config(config: Mapping[str, Any]) -> dict[str, Any]:
    channels = config.get("channels") if isinstance(config, Mapping) else None
    if not isinstance(channels, Mapping):
        return {}
    data = channels.get(CHANNEL_FEISHU)
    return dict(data) if isinstance(data, Mapping) else {}
