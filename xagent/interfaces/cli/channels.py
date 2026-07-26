"""Strict channel configuration helpers for the schema-version 2 runtime."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from ...settings import XAgentSettings


CHANNEL_API = "api"
CHANNEL_FEISHU = "feishu"
CHANNEL_WEIXIN = "weixin"
CHANNEL_VOICE = "voice"
VALID_CHANNELS = {CHANNEL_API, CHANNEL_FEISHU, CHANNEL_WEIXIN, CHANNEL_VOICE}


def load_config_file(config_dir: Path) -> dict[str, Any]:
    """Load and validate the only supported configuration schema."""
    return XAgentSettings.load(config_dir / "config.yaml").model_dump(
        mode="python",
        exclude_none=True,
    )


def _channel_config(config: Mapping[str, Any], channel: str) -> dict[str, Any]:
    channels = config["channels"]
    data = channels[channel]
    return dict(data)


def api_config(config: Mapping[str, Any]) -> dict[str, Any]:
    return _channel_config(config, CHANNEL_API)


def feishu_config(config: Mapping[str, Any]) -> dict[str, Any]:
    data = _channel_config(config, CHANNEL_FEISHU)
    data.pop("enabled", None)
    return data


def weixin_config(config: Mapping[str, Any]) -> dict[str, Any]:
    data = _channel_config(config, CHANNEL_WEIXIN)
    data.pop("enabled", None)
    return data


def voice_config(config: Mapping[str, Any]) -> dict[str, Any]:
    return _channel_config(config, CHANNEL_VOICE)
