"""Configuration loader for the Feishu adapter."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class FeishuAdapterConfig:
    """User-facing configuration for the Feishu adapter.

    The adapter behaves like a human teammate by default:

    * ``p2p`` direct chats: always reply.
    * ``group`` / ``topic`` with @bot: reply.
    * ``group`` / ``topic`` without @bot: listen first, then let the agent
      decide whether to speak. Set ``group_reply_only_when_mentioned`` to
      true for conservative rooms where unmentioned messages should only be
      recorded, never answered.

    Only credentials and a handful of operational defaults are configurable.

    Attributes:
        app_id: Feishu app id (``cli_xxx``). Required.
        app_secret: Feishu app secret. Required.
        domain: ``feishu`` (default), ``lark``, or a full custom domain.
        log_level: One of ``debug``, ``info``, ``warn``, ``error``.
        stream: Use Feishu streaming cards to incrementally update the
            current segmented reply message.
        group_fetch_limit: How many recent Feishu group/topic messages to
            pull for each routed group/topic message. ``0`` disables history
            pulls.
        group_fetch_timeout: Maximum seconds to wait for Feishu history.
        group_reply_only_when_mentioned: Record unmentioned group/topic
            messages but never reply to them. Defaults to false.
    """

    app_id: str
    app_secret: str
    domain: Optional[str] = None
    log_level: str = "info"

    stream: bool = False

    group_fetch_limit: int = 10
    group_fetch_timeout: float = 5.0
    group_reply_only_when_mentioned: bool = False

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FeishuAdapterConfig":
        known_fields = {field.name for field in cls.__dataclass_fields__.values()}
        unsupported_keys = sorted(set(data) - known_fields)
        if unsupported_keys:
            raise ValueError(
                f"Unsupported Feishu config key(s): {', '.join(unsupported_keys)}"
            )
        app_id = str(data.get("app_id") or "").strip()
        app_secret = str(data.get("app_secret") or "").strip()
        if not app_id or not app_secret:
            raise ValueError("Feishu config requires app_id and app_secret")
        kwargs = dict(data)
        kwargs.update(app_id=app_id, app_secret=app_secret)
        return cls(**kwargs)
