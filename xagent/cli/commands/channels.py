"""Channel names and normalization for the xAgent CLI."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

import yaml


CHANNEL_API = "api"
CHANNEL_FEISHU = "feishu"
CHANNEL_WEIXIN = "weixin"
CHANNEL_VOICE = "voice"
VALID_CHANNELS = {CHANNEL_API, CHANNEL_FEISHU, CHANNEL_WEIXIN, CHANNEL_VOICE}


def _feishu_channel_enabled(config: Mapping[str, Any]) -> bool:
    if "enabled" in config:
        return bool(config.get("enabled"))
    return bool(config.get("app_id") and config.get("app_secret"))


def _weixin_channel_enabled(config: Mapping[str, Any]) -> bool:
    if "enabled" in config:
        return bool(config.get("enabled"))
    return bool(config.get("account_id"))


def _voice_channel_enabled(config: Mapping[str, Any]) -> bool:
    if "enabled" in config:
        return bool(config.get("enabled"))
    return bool(config)


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
    """Return public managed channels enabled by config."""
    channels = config.get("channels") if isinstance(config, Mapping) else None
    if not isinstance(channels, Mapping):
        return [CHANNEL_API]

    api_cfg = channels.get(CHANNEL_API)
    api_enabled = isinstance(api_cfg, Mapping) and bool(api_cfg.get("enabled", True))

    result: list[str] = []
    if api_enabled:
        result.append(CHANNEL_API)

    feishu_cfg = channels.get(CHANNEL_FEISHU)
    feishu_cfg = feishu_cfg if isinstance(feishu_cfg, Mapping) else {}
    if _feishu_channel_enabled(feishu_cfg):
        result.append(CHANNEL_FEISHU)

    weixin_cfg = channels.get(CHANNEL_WEIXIN)
    weixin_cfg = weixin_cfg if isinstance(weixin_cfg, Mapping) else {}
    if _weixin_channel_enabled(weixin_cfg):
        result.append(CHANNEL_WEIXIN)

    voice_cfg = channels.get(CHANNEL_VOICE)
    voice_cfg = voice_cfg if isinstance(voice_cfg, Mapping) else {}
    if _voice_channel_enabled(voice_cfg):
        result.append(CHANNEL_VOICE)

    return result


def default_start_channel_from_config(config: Optional[Mapping[str, Any]]) -> str:
    """Choose the safest implicit channel for run/start commands."""
    enabled = enabled_channels_from_config(config)
    if not enabled:
        raise ChannelSelectionError(
            "No enabled channels found. Configure channels.api, channels.feishu, channels.weixin, "
            "or channels.voice, "
            "or pass --channel explicitly."
        )
    if CHANNEL_API in enabled:
        return CHANNEL_API
    return enabled[0]


def normalize_channel_values(
    values: Optional[Sequence[str]],
    *,
    default: str,
    config: Optional[Mapping[str, Any]] = None,
) -> list[str]:
    """Normalize comma-separated/repeated channel values into public channels."""
    del config
    raw_values: Iterable[str] = values if values else (default,)
    selected: list[str] = []
    for raw_value in raw_values:
        for token in str(raw_value).split(","):
            channel = token.strip().lower()
            if not channel:
                continue
            if channel not in VALID_CHANNELS:
                valid = ", ".join(sorted(VALID_CHANNELS))
                raise ChannelSelectionError(f"Unknown channel {channel!r}. Expected one of: {valid}.")
            if channel not in selected:
                selected.append(channel)

    if not selected:
        raise ChannelSelectionError(
            "No enabled channels found. Configure channels.api, channels.feishu, channels.weixin, "
            "or channels.voice, "
            "or pass --channel explicitly."
        )
    return selected


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
    if not isinstance(data, Mapping):
        return {}

    runtime_config = dict(data)
    runtime_config.pop("enabled", None)
    return runtime_config


def weixin_config(config: Mapping[str, Any]) -> dict[str, Any]:
    channels = config.get("channels") if isinstance(config, Mapping) else None
    if not isinstance(channels, Mapping):
        return {}
    data = channels.get(CHANNEL_WEIXIN)
    if not isinstance(data, Mapping):
        return {}

    runtime_config = dict(data)
    runtime_config.pop("enabled", None)
    return runtime_config


def voice_config(config: Mapping[str, Any]) -> dict[str, Any]:
    channels = config.get("channels") if isinstance(config, Mapping) else None
    if not isinstance(channels, Mapping):
        return {}
    data = channels.get(CHANNEL_VOICE)
    return dict(data) if isinstance(data, Mapping) else {}
