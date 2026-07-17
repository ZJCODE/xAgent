"""Provider defaults for model API protocol selection."""

from __future__ import annotations

from dataclasses import dataclass
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

REASONING_EFFORT_MINIMAL = "minimal"
REASONING_EFFORT_LOW = "low"
REASONING_EFFORT_MEDIUM = "medium"
REASONING_EFFORT_HIGH = "high"
REASONING_EFFORT_XHIGH = "xhigh"
REASONING_EFFORT_MAX = "max"

OPENAI_RESPONSES_REASONING_EFFORTS = (
    REASONING_EFFORT_MINIMAL,
    REASONING_EFFORT_LOW,
    REASONING_EFFORT_MEDIUM,
    REASONING_EFFORT_HIGH,
    REASONING_EFFORT_XHIGH,
)
OPENAI_CHAT_REASONING_EFFORTS = OPENAI_RESPONSES_REASONING_EFFORTS + (
    REASONING_EFFORT_MAX,
)
DEEPSEEK_REASONING_EFFORTS = (
    REASONING_EFFORT_HIGH,
    REASONING_EFFORT_MAX,
)
ANTHROPIC_REASONING_EFFORTS = (
    REASONING_EFFORT_LOW,
    REASONING_EFFORT_MEDIUM,
    REASONING_EFFORT_HIGH,
    REASONING_EFFORT_XHIGH,
    REASONING_EFFORT_MAX,
)


@dataclass(frozen=True)
class ReasoningConfig:
    """Provider-neutral reasoning controls normalized from ``config.yaml``."""

    enabled: bool
    effort: Optional[str] = None
    budget_tokens: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"enabled": self.enabled}
        if self.effort is not None:
            data["effort"] = self.effort
        if self.budget_tokens is not None:
            data["budget_tokens"] = self.budget_tokens
        return data


@dataclass(frozen=True)
class ReasoningCapability:
    """Static controls known to work for a provider/API combination."""

    supported: bool
    controls: tuple[str, ...] = ()
    effort_values: tuple[str, ...] = ()
    min_budget_tokens: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "supported": self.supported,
            "controls": list(self.controls),
            "effort_values": list(self.effort_values),
        }
        if self.min_budget_tokens is not None:
            data["min_budget_tokens"] = self.min_budget_tokens
        return data


def normalize_provider_name(provider_name: Optional[str]) -> str:
    return str(provider_name or "").strip().lower()


def normalize_model_api(model_api: Optional[str]) -> str:
    normalized = str(model_api or "").strip().lower()
    if normalized not in MODEL_APIS:
        allowed = ", ".join(sorted(MODEL_APIS))
        raise ValueError(f"provider.model_api must be one of: {allowed}")
    return normalized


def _model_api_from_hint(model_api: Optional[str]) -> str:
    if model_api is None:
        return MODEL_API_OPENAI_CHAT_COMPLETIONS
    return normalize_model_api(model_api)


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
    if "supports_vision" in provider_cfg:
        return provider_cfg.get("supports_vision") is True
    provider = normalize_provider_name(provider_cfg.get("name"))
    if provider == PROVIDER_CUSTOM:
        return False
    if provider in VISION_CAPABLE_PROVIDERS:
        return True
    if provider:
        return False
    return provider_is_official_openai(provider_cfg)


def provider_model_api(provider_cfg: dict[str, Any]) -> str:
    configured_model_api = provider_cfg.get("model_api")
    if configured_model_api is not None:
        return normalize_model_api(configured_model_api)

    provider = normalize_provider_name(provider_cfg.get("name"))
    if provider in PROVIDER_MODEL_APIS:
        return PROVIDER_MODEL_APIS[provider]

    if provider_is_official_openai(provider_cfg):
        return MODEL_API_OPENAI_RESPONSES
    return MODEL_API_OPENAI_CHAT_COMPLETIONS


def resolved_provider_name(provider_cfg: dict[str, Any]) -> str:
    """Resolve legacy unnamed provider configs for request-specific behavior."""
    provider = normalize_provider_name(provider_cfg.get("name"))
    if provider:
        return provider
    return PROVIDER_OPENAI if provider_is_official_openai(provider_cfg) else PROVIDER_CUSTOM


def reasoning_capability(
    provider_name: str,
    model_api: Optional[str] = None,
) -> ReasoningCapability:
    """Return the framework-supported reasoning controls for one endpoint."""
    provider = normalize_provider_name(provider_name)
    if provider == PROVIDER_MINIMAX:
        return ReasoningCapability(supported=False)
    if provider == PROVIDER_OPENAI:
        return ReasoningCapability(
            supported=True,
            controls=("effort",),
            effort_values=OPENAI_RESPONSES_REASONING_EFFORTS,
        )
    if provider == PROVIDER_DEEPSEEK:
        return ReasoningCapability(
            supported=True,
            controls=("effort",),
            effort_values=DEEPSEEK_REASONING_EFFORTS,
        )
    if provider == PROVIDER_QWEN:
        return ReasoningCapability(
            supported=True,
            controls=("budget_tokens",),
            min_budget_tokens=1,
        )
    if provider == PROVIDER_ANTHROPIC:
        return ReasoningCapability(
            supported=True,
            controls=("effort", "budget_tokens"),
            effort_values=ANTHROPIC_REASONING_EFFORTS,
            min_budget_tokens=1024,
        )

    selected_api = _model_api_from_hint(model_api)
    if selected_api == MODEL_API_ANTHROPIC_MESSAGES:
        return ReasoningCapability(
            supported=True,
            controls=("effort", "budget_tokens"),
            effort_values=ANTHROPIC_REASONING_EFFORTS,
            min_budget_tokens=1024,
        )
    if selected_api == MODEL_API_OPENAI_RESPONSES:
        return ReasoningCapability(
            supported=True,
            controls=("effort",),
            effort_values=OPENAI_RESPONSES_REASONING_EFFORTS,
        )
    return ReasoningCapability(
        supported=True,
        controls=("effort",),
        effort_values=OPENAI_CHAT_REASONING_EFFORTS,
    )


def provider_reasoning_capability(provider_cfg: dict[str, Any]) -> ReasoningCapability:
    return reasoning_capability(
        resolved_provider_name(provider_cfg),
        provider_model_api(provider_cfg),
    )


def normalize_reasoning_config(provider_cfg: dict[str, Any]) -> Optional[ReasoningConfig]:
    """Validate and normalize the optional provider-level reasoning block."""
    raw = provider_cfg.get("reasoning")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("provider.reasoning must be a dictionary")

    unsupported = sorted(set(raw) - {"enabled", "effort", "budget_tokens"})
    if unsupported:
        raise ValueError(f"Unsupported provider.reasoning key(s): {', '.join(unsupported)}")
    if "enabled" not in raw or not isinstance(raw.get("enabled"), bool):
        raise ValueError("provider.reasoning.enabled must be a boolean")

    enabled = raw["enabled"]
    effort_value = raw.get("effort")
    budget_value = raw.get("budget_tokens")
    has_effort = effort_value is not None
    has_budget = budget_value is not None

    if has_effort:
        if not isinstance(effort_value, str) or not effort_value.strip():
            raise ValueError("provider.reasoning.effort must be a non-empty string")
        effort = effort_value.strip().lower()
    else:
        effort = None
    if has_budget:
        if isinstance(budget_value, bool) or not isinstance(budget_value, int) or budget_value <= 0:
            raise ValueError("provider.reasoning.budget_tokens must be a positive integer")
        budget_tokens = budget_value
    else:
        budget_tokens = None

    if enabled and has_effort == has_budget:
        raise ValueError(
            "provider.reasoning requires exactly one of effort or budget_tokens when enabled"
        )
    if not enabled and (has_effort or has_budget):
        raise ValueError(
            "provider.reasoning.effort and budget_tokens are not allowed when reasoning is disabled"
        )

    capability = provider_reasoning_capability(provider_cfg)
    provider = resolved_provider_name(provider_cfg)
    if not capability.supported:
        raise ValueError(f"provider.reasoning is not supported for provider {provider}")
    if effort is not None:
        if "effort" not in capability.controls:
            raise ValueError(f"provider.reasoning.effort is not supported for provider {provider}")
        if effort not in capability.effort_values:
            allowed = ", ".join(capability.effort_values)
            raise ValueError(
                f"provider.reasoning.effort for provider {provider} must be one of: {allowed}"
            )
    if budget_tokens is not None:
        if "budget_tokens" not in capability.controls:
            raise ValueError(
                f"provider.reasoning.budget_tokens is not supported for provider {provider}"
            )
        minimum = capability.min_budget_tokens or 1
        if budget_tokens < minimum:
            raise ValueError(
                f"provider.reasoning.budget_tokens for provider {provider} must be at least {minimum}"
            )
        if provider_model_api(provider_cfg) == MODEL_API_ANTHROPIC_MESSAGES:
            max_tokens = provider_cfg.get("max_tokens", 8192)
            if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and budget_tokens >= max_tokens:
                raise ValueError(
                    "provider.reasoning.budget_tokens must be less than provider.max_tokens "
                    f"(effective value: {max_tokens})"
                )

    return ReasoningConfig(
        enabled=enabled,
        effort=effort,
        budget_tokens=budget_tokens,
    )


def model_api_uses_anthropic_client(model_api: str) -> bool:
    return normalize_model_api(model_api) == MODEL_API_ANTHROPIC_MESSAGES


def model_api_uses_openai_client(model_api: str) -> bool:
    return not model_api_uses_anthropic_client(model_api)
