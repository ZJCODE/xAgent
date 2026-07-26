"""Configuration loader for the Weixin iLink adapter."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
WEIXIN_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"

@dataclass
class WeixinAdapterConfig:
    """User-facing configuration for the Weixin iLink channel.

    Credentials are intentionally not stored here. ``account_id`` selects the
    credential state in the Agent SQLite database.
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
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WeixinAdapterConfig":
        known_fields = {field.name for field in cls.__dataclass_fields__.values()}
        unsupported_keys = sorted(set(data) - known_fields)
        if unsupported_keys:
            raise ValueError(
                f"Unsupported Weixin config key(s): {', '.join(unsupported_keys)}"
            )
        account_id = str(data.get("account_id") or "").strip()
        if not account_id:
            raise ValueError("Weixin config requires 'account_id'. Run: xagent channel setup weixin")
        kwargs = dict(data)
        kwargs["account_id"] = account_id
        kwargs["owner_user_id"] = str(data.get("owner_user_id") or "").strip()
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
