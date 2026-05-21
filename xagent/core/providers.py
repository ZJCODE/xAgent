"""Provider defaults for model API protocol selection."""

from __future__ import annotations

from typing import Any, Optional

MODEL_API_OPENAI_RESPONSES = "openai_responses"
MODEL_API_OPENAI_CHAT_COMPLETIONS = "openai_chat_completions"
MODEL_API_ANTHROPIC_MESSAGES = "anthropic_messages"

MODEL_APIS = {
    MODEL_API_OPENAI_RESPONSES,
    MODEL_API_OPENAI_CHAT_COMPLETIONS,
    MODEL_API_ANTHROPIC_MESSAGES,
}

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

PROVIDER_MODEL_APIS = {
    PROVIDER_OPENAI: MODEL_API_OPENAI_RESPONSES,
    PROVIDER_DEEPSEEK: MODEL_API_OPENAI_CHAT_COMPLETIONS,
    PROVIDER_MINIMAX: MODEL_API_ANTHROPIC_MESSAGES,
    PROVIDER_QWEN: MODEL_API_OPENAI_CHAT_COMPLETIONS,
    PROVIDER_ANTHROPIC: MODEL_API_ANTHROPIC_MESSAGES,
}

PROVIDER_BASE_URLS = {
    PROVIDER_OPENAI: "https://api.openai.com/v1",
    PROVIDER_DEEPSEEK: "https://api.deepseek.com",
    PROVIDER_MINIMAX: "https://api.minimaxi.com/anthropic",
    PROVIDER_QWEN: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    PROVIDER_ANTHROPIC: "https://api.anthropic.com",
}

VISION_CAPABLE_PROVIDERS = frozenset({
    PROVIDER_OPENAI,
    PROVIDER_QWEN,
})

CUSTOM_BASE_URLS = {
    MODEL_API_OPENAI_RESPONSES: "https://api.example.com/v1",
    MODEL_API_OPENAI_CHAT_COMPLETIONS: "https://api.example.com/v1",
    MODEL_API_ANTHROPIC_MESSAGES: "https://api.example.com/anthropic",
}


def normalize_provider_name(provider_name: Optional[str]) -> str:
    return str(provider_name or "").strip().lower()


def normalize_model_api(model_api: Optional[str]) -> str:
    normalized = str(model_api or "").strip().lower()
    if normalized not in MODEL_APIS:
        allowed = ", ".join(sorted(MODEL_APIS))
        raise ValueError(f"provider.model_api must be one of: {allowed}")
    return normalized


def legacy_sdk_model_api(sdk: Optional[str]) -> str:
    normalized = str(sdk or "").strip().lower()
    if normalized == "openai":
        return MODEL_API_OPENAI_CHAT_COMPLETIONS
    if normalized == "anthropic":
        return MODEL_API_ANTHROPIC_MESSAGES
    raise ValueError("provider.sdk must be one of: openai, anthropic")


def _model_api_from_hint(model_api: Optional[str]) -> str:
    if model_api is None:
        return MODEL_API_OPENAI_CHAT_COMPLETIONS
    try:
        return normalize_model_api(model_api)
    except ValueError:
        return legacy_sdk_model_api(model_api)


def provider_base_url(provider_name: str, model_api: Optional[str] = None) -> str:
    provider = normalize_provider_name(provider_name)
    if provider == PROVIDER_CUSTOM:
        return CUSTOM_BASE_URLS[_model_api_from_hint(model_api)]
    return PROVIDER_BASE_URLS.get(provider, CUSTOM_BASE_URLS[MODEL_API_OPENAI_CHAT_COMPLETIONS])


def provider_is_official_openai(provider_cfg: dict[str, Any]) -> bool:
    provider_name = normalize_provider_name(provider_cfg.get("name"))
    if provider_name:
        return provider_name == PROVIDER_OPENAI

    base_url = str(provider_cfg.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        return True
    return base_url == PROVIDER_BASE_URLS[PROVIDER_OPENAI].rstrip("/")


def provider_supports_vision(provider_cfg: dict[str, Any]) -> bool:
    provider = normalize_provider_name(provider_cfg.get("name"))
    if provider == PROVIDER_CUSTOM:
        return provider_cfg.get("supports_vision") is True
    if provider in VISION_CAPABLE_PROVIDERS:
        return True
    if provider:
        return False
    return provider_is_official_openai(provider_cfg)


def provider_model_api(provider_cfg: dict[str, Any]) -> str:
    configured_model_api = provider_cfg.get("model_api")
    if configured_model_api is not None:
        return normalize_model_api(configured_model_api)

    legacy_sdk = provider_cfg.get("sdk")
    if legacy_sdk is not None:
        return legacy_sdk_model_api(legacy_sdk)

    provider = normalize_provider_name(provider_cfg.get("name"))
    if provider in PROVIDER_MODEL_APIS:
        return PROVIDER_MODEL_APIS[provider]

    if provider_is_official_openai(provider_cfg):
        return MODEL_API_OPENAI_RESPONSES
    return MODEL_API_OPENAI_CHAT_COMPLETIONS


def model_api_uses_anthropic_client(model_api: str) -> bool:
    return normalize_model_api(model_api) == MODEL_API_ANTHROPIC_MESSAGES


def model_api_uses_openai_client(model_api: str) -> bool:
    return not model_api_uses_anthropic_client(model_api)
