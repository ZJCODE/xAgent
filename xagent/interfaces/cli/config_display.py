"""Safe display helpers for config and secrets."""

from __future__ import annotations

import re
from typing import Any

import yaml

from ...tools.search_tool import is_placeholder_api_key

_SECRET_KEY_PATTERN = re.compile(
    r"(api[_-]?key|app[_-]?secret|secret|token|password|access[_-]?key)",
    re.IGNORECASE,
)


def _looks_like_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY_PATTERN.search(str(key or "")))


def redact_secret_value(value: str) -> str:
    raw = str(value or "")
    if not raw.strip():
        return raw
    if is_placeholder_api_key(raw):
        return raw
    if len(raw) <= 8:
        return "***"
    return f"{raw[:4]}…{raw[-4:]}"


def redact_config_object(data: Any) -> Any:
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for key, value in data.items():
            if _looks_like_secret_key(str(key)) and isinstance(value, str):
                out[key] = redact_secret_value(value)
            else:
                out[key] = redact_config_object(value)
        return out
    if isinstance(data, list):
        return [redact_config_object(item) for item in data]
    return data


def _contains_redactable_secrets(data: Any) -> bool:
    if isinstance(data, dict):
        for key, value in data.items():
            if _looks_like_secret_key(str(key)) and isinstance(value, str):
                if value.strip() and not is_placeholder_api_key(value):
                    return True
            if _contains_redactable_secrets(value):
                return True
        return False
    if isinstance(data, list):
        return any(_contains_redactable_secrets(item) for item in data)
    return False


def format_config_for_display(raw_text: str, *, reveal_secrets: bool = False) -> tuple[str, bool]:
    if reveal_secrets:
        return raw_text, False
    try:
        parsed = yaml.safe_load(raw_text)
    except yaml.YAMLError:
        return raw_text, False
    if not isinstance(parsed, dict):
        return raw_text, False
    redacted = redact_config_object(parsed)
    text = yaml.safe_dump(redacted, sort_keys=False, allow_unicode=True)
    return text, _contains_redactable_secrets(parsed)
