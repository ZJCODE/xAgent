"""Configuration loader for the Weixin iLink adapter."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
WEIXIN_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand_env(value: Any) -> Any:
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        env_value = os.environ.get(name)
        if env_value is None:
            raise ValueError(f"Environment variable {name!r} is not set")
        return env_value

    return _ENV_PATTERN.sub(replace, value)


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _coerce_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _non_negative_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


@dataclass
class WeixinAdapterConfig:
    """User-facing configuration for the Weixin iLink channel.

    Credentials are intentionally not stored here. ``account_id`` selects the
    credential/state files written by ``xagent channel weixin setup``.
    """

    account_id: str
    owner_user_id: str = ""
    base_url: str = ILINK_BASE_URL
    cdn_base_url: str = WEIXIN_CDN_BASE_URL
    bot_type: str = "3"
    channel_version: str = "1.0.0"

    owner_only: bool = True
    allow_users: list[str] = field(default_factory=list)

    send_typing: bool = True
    typing_keepalive_seconds: float = 5.0
    typing_ticket_ttl_seconds: float = 3600.0

    text_max_chars: int = 2000
    send_chunk_delay_seconds: float = 0.8
    send_retries: int = 2
    send_retry_delay_seconds: float = 1.0

    poll_timeout_ms: int = 35_000
    api_timeout_ms: int = 15_000
    qr_timeout_seconds: int = 300
    retry_delay_seconds: float = 2.0
    backoff_delay_seconds: float = 30.0
    max_consecutive_failures: int = 3

    media_enabled: bool = True
    advanced: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> "WeixinAdapterConfig":
        config_path = Path(path).expanduser().resolve()
        if not config_path.is_file():
            raise FileNotFoundError(f"Weixin config not found: {config_path}")
        with config_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"Weixin config must be a YAML mapping: {config_path}")
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WeixinAdapterConfig":
        expanded = {key: _expand_env(value) for key, value in data.items()}
        known_fields = {field.name for field in cls.__dataclass_fields__.values()}
        unsupported_keys = sorted(set(expanded) - known_fields)
        if unsupported_keys:
            joined = ", ".join(unsupported_keys)
            raise ValueError(f"Unsupported Weixin config key(s): {joined}")

        account_id = str(expanded.get("account_id") or os.environ.get("WEIXIN_ACCOUNT_ID") or "").strip()
        if not account_id:
            raise ValueError("Weixin config requires 'account_id'. Run: xagent weixin setup")

        kwargs: Dict[str, Any] = dict(expanded)
        kwargs["account_id"] = account_id
        kwargs["owner_user_id"] = str(
            kwargs.get("owner_user_id") or os.environ.get("WEIXIN_OWNER_USER_ID") or ""
        ).strip()
        kwargs["base_url"] = str(kwargs.get("base_url") or ILINK_BASE_URL).strip().rstrip("/")
        kwargs["cdn_base_url"] = str(kwargs.get("cdn_base_url") or WEIXIN_CDN_BASE_URL).strip().rstrip("/")
        kwargs["bot_type"] = str(kwargs.get("bot_type") or "3").strip() or "3"
        kwargs["channel_version"] = str(kwargs.get("channel_version") or "1.0.0").strip() or "1.0.0"

        kwargs["owner_only"] = _coerce_bool(kwargs.get("owner_only"), True)
        kwargs["send_typing"] = _coerce_bool(kwargs.get("send_typing"), True)
        kwargs["media_enabled"] = _coerce_bool(kwargs.get("media_enabled"), True)
        kwargs["allow_users"] = _coerce_str_list(kwargs.get("allow_users"))

        kwargs["text_max_chars"] = _positive_int(kwargs.get("text_max_chars"), 2000)
        kwargs["send_retries"] = _non_negative_int(kwargs.get("send_retries"), 2)
        kwargs["poll_timeout_ms"] = _positive_int(kwargs.get("poll_timeout_ms"), 35_000)
        kwargs["api_timeout_ms"] = _positive_int(kwargs.get("api_timeout_ms"), 15_000)
        kwargs["qr_timeout_seconds"] = _positive_int(kwargs.get("qr_timeout_seconds"), 300)
        kwargs["max_consecutive_failures"] = _positive_int(kwargs.get("max_consecutive_failures"), 3)

        for key, default in (
            ("typing_keepalive_seconds", 5.0),
            ("typing_ticket_ttl_seconds", 3600.0),
            ("send_chunk_delay_seconds", 0.8),
            ("send_retry_delay_seconds", 1.0),
            ("retry_delay_seconds", 2.0),
            ("backoff_delay_seconds", 30.0),
        ):
            kwargs[key] = _positive_float(kwargs.get(key), default)

        kwargs["advanced"] = dict(kwargs.get("advanced") or {})
        return cls(**kwargs)


def weixin_channel_config_from_selection(
    *,
    account_id: str,
    owner_user_id: str,
    base_url: str = ILINK_BASE_URL,
    cdn_base_url: str = WEIXIN_CDN_BASE_URL,
    owner_only: bool = True,
    allow_users: Optional[list[str]] = None,
    media_enabled: bool = True,
) -> dict[str, Any]:
    config: dict[str, Any] = {
        "account_id": account_id,
        "owner_user_id": owner_user_id,
        "base_url": base_url.rstrip("/"),
        "cdn_base_url": cdn_base_url.rstrip("/"),
        "owner_only": owner_only,
        "media_enabled": media_enabled,
        "send_typing": True,
    }
    if allow_users:
        config["allow_users"] = allow_users
    return config
