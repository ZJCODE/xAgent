"""Configuration loader for the Feishu adapter."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand_env(value: Any) -> Any:
    """Expand ``${ENV_VAR}`` references inside string config values."""
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        env_value = os.environ.get(name)
        if env_value is None:
            raise ValueError(f"Environment variable {name!r} is not set")
        return env_value

    return _ENV_PATTERN.sub(replace, value)


@dataclass
class FeishuAdapterConfig:
    """User-facing configuration for the Feishu adapter.

    The adapter behaves like a human teammate by default — no behavioral
    knobs are exposed:

    * ``p2p`` direct chats: always reply.
    * ``group`` / ``topic`` with @bot: reply.
    * ``group`` / ``topic`` without @bot: ignore.

    Only credentials and a handful of operational defaults are configurable.

    Attributes:
        app_id: Feishu app id (``cli_xxx``). Required.
        app_secret: Feishu app secret. Required.
        domain: ``feishu`` (default), ``lark``, or a full custom domain.
        log_level: One of ``debug``, ``info``, ``warn``, ``error``.
        stream: Use Feishu streaming cards for replies. Requires the agent
            output to be streamable text (no ``output_schema``).
        enable_memory: Pass-through to the agent's long-term memory.
        history_count / max_iter / max_concurrent_tools: Per-turn knobs
            forwarded to ``agent.chat``.
        group_history_count: How many recent Feishu group/topic messages to
            pull on each @mention. ``0`` disables history pulls.
        history_fetch_timeout: Maximum seconds to wait for Feishu history.
        advanced: Raw pass-through kwargs for ``FeishuChannel`` (policy,
            safety, ...). Reserved for power users.
    """

    app_id: str
    app_secret: str
    domain: Optional[str] = None
    log_level: str = "info"

    stream: bool = False
    enable_memory: bool = True

    history_count: Optional[int] = None
    max_iter: Optional[int] = None
    max_concurrent_tools: Optional[int] = None

    group_history_count: int = 10
    history_fetch_timeout: float = 5.0

    advanced: Dict[str, Any] = field(default_factory=dict)

    # --- factory helpers --------------------------------------------------

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> "FeishuAdapterConfig":
        """Load configuration from a YAML file with env-var expansion."""
        config_path = Path(path).expanduser().resolve()
        if not config_path.is_file():
            raise FileNotFoundError(f"Feishu config not found: {config_path}")
        with config_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"Feishu config must be a YAML mapping: {config_path}")
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FeishuAdapterConfig":
        expanded = {k: _expand_env(v) for k, v in data.items()}

        app_id = expanded.get("app_id") or os.environ.get("LARK_APP_ID")
        app_secret = expanded.get("app_secret") or os.environ.get("LARK_APP_SECRET")
        if not app_id or not app_secret:
            raise ValueError(
                "Feishu config requires 'app_id' and 'app_secret' "
                "(or LARK_APP_ID / LARK_APP_SECRET environment variables)."
            )

        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        kwargs: Dict[str, Any] = {}
        advanced: Dict[str, Any] = dict(expanded.get("advanced") or {})
        for key, value in expanded.items():
            if key == "advanced":
                continue
            if key in known_fields:
                kwargs[key] = value
            # Silently drop legacy / unknown top-level keys instead of
            # forwarding them as FeishuChannel kwargs (which would raise).

        kwargs["app_id"] = app_id
        kwargs["app_secret"] = app_secret
        kwargs["advanced"] = advanced
        return cls(**kwargs)
