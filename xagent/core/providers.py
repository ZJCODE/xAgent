"""Provider defaults for model SDK selection."""

from __future__ import annotations

from typing import Any, Optional


SDK_OPENAI = "openai"
SDK_ANTHROPIC = "anthropic"

PROVIDER_OPENAI = "openai"
PROVIDER_DEEPSEEK = "deepseek"
PROVIDER_MINIMAX = "minimax"
PROVIDER_QWEN = "qwen"
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_CUSTOM = "custom"

KNOWN_PROVIDERS = (
    PROVIDER_OPENAI,
    PROVIDER_DEEPSEEK,
    PROVIDER_MINIMAX,
    PROVIDER_QWEN,
    PROVIDER_ANTHROPIC,
    PROVIDER_CUSTOM,
)

PROVIDER_SDKS = {
    PROVIDER_OPENAI: SDK_OPENAI,
    PROVIDER_DEEPSEEK: SDK_OPENAI,
    PROVIDER_MINIMAX: SDK_ANTHROPIC,
    PROVIDER_QWEN: SDK_OPENAI,
    PROVIDER_ANTHROPIC: SDK_ANTHROPIC,
}

PROVIDER_BASE_URLS = {
    PROVIDER_OPENAI: "https://api.openai.com/v1",
    PROVIDER_DEEPSEEK: "https://api.deepseek.com",
    PROVIDER_MINIMAX: "https://api.minimaxi.com/anthropic",
    PROVIDER_QWEN: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    PROVIDER_ANTHROPIC: "https://api.anthropic.com",
}

CUSTOM_BASE_URLS = {
    SDK_OPENAI: "https://api.example.com/v1",
    SDK_ANTHROPIC: "https://api.example.com/anthropic",
}


def normalize_sdk(sdk: Optional[str]) -> str:
    normalized = str(sdk or "").strip().lower()
    if normalized not in {SDK_OPENAI, SDK_ANTHROPIC}:
        raise ValueError("provider.sdk must be one of: openai, anthropic")
    return normalized


def normalize_provider_name(provider_name: Optional[str]) -> str:
    return str(provider_name or "").strip().lower()


def provider_sdk(provider_name: Optional[str], configured_sdk: Optional[str] = None) -> str:
    provider = normalize_provider_name(provider_name)
    if provider == PROVIDER_CUSTOM:
        if configured_sdk is None:
            raise ValueError("provider.sdk is required when provider.name is custom")
        return normalize_sdk(configured_sdk)
    if provider in PROVIDER_SDKS:
        return PROVIDER_SDKS[provider]
    if configured_sdk is not None:
        return normalize_sdk(configured_sdk)
    return SDK_OPENAI


def provider_base_url(provider_name: str, sdk: Optional[str] = None) -> str:
    provider = normalize_provider_name(provider_name)
    if provider == PROVIDER_CUSTOM:
        return CUSTOM_BASE_URLS[normalize_sdk(sdk or SDK_OPENAI)]
    return PROVIDER_BASE_URLS.get(provider, CUSTOM_BASE_URLS[SDK_OPENAI])


def provider_config_sdk(provider_cfg: dict[str, Any]) -> str:
    return provider_sdk(provider_cfg.get("name"), provider_cfg.get("sdk"))
