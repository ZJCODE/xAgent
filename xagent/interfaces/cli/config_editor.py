"""Intent-focused config updates for the launcher."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

from ...core.providers import (
    KNOWN_PROVIDERS,
    MODEL_API_ANTHROPIC_MESSAGES,
    MODEL_API_OPENAI_CHAT_COMPLETIONS,
    MODEL_API_OPENAI_RESPONSES,
    PROVIDER_CUSTOM,
    PROVIDER_MINIMAX,
    PROVIDER_OPENAI,
    PROVIDER_QWEN,
    model_api_uses_openai_client,
    normalize_model_api,
    normalize_provider_name,
    provider_base_url,
    provider_model_api,
)
from ...tools.image_generation_tool import (
    DEFAULT_IMAGE_GENERATION_MODEL,
    DEFAULT_IMAGE_GENERATION_QUALITY,
    DEFAULT_IMAGE_GENERATION_SIZE,
    DEFAULT_MINIMAX_IMAGE_GENERATION_ASPECT_RATIO,
    DEFAULT_MINIMAX_IMAGE_GENERATION_MODEL,
    DEFAULT_QWEN_IMAGE_GENERATION_MODEL,
    DEFAULT_QWEN_IMAGE_GENERATION_SIZE,
    IMAGE_GENERATION_PROVIDER_MINIMAX,
    IMAGE_GENERATION_PROVIDER_NONE,
    IMAGE_GENERATION_PROVIDER_OPENAI,
    IMAGE_GENERATION_PROVIDER_QWEN,
    normalize_image_generation_provider,
)
from ...tools.search_tool import is_placeholder_api_key, normalize_search_provider, SEARCH_PROVIDER_BUILTIN
from ...voice.config import (
    QWEN_KEY_PLACEHOLDER,
    SONIOX_KEY_PLACEHOLDER,
    VOICE_PROVIDER_CUSTOM,
    VOICE_PROVIDER_QWEN,
    VOICE_PROVIDER_SONIOX,
    VoiceChannelConfig,
)
from ..base import BaseAgentConfig, BaseAgentRunner


SEARCH_PROVIDERS = (SEARCH_PROVIDER_BUILTIN, PROVIDER_OPENAI, PROVIDER_QWEN, PROVIDER_MINIMAX)
IMAGE_GENERATION_PROVIDERS = (
    IMAGE_GENERATION_PROVIDER_NONE,
    IMAGE_GENERATION_PROVIDER_OPENAI,
    IMAGE_GENERATION_PROVIDER_MINIMAX,
    IMAGE_GENERATION_PROVIDER_QWEN,
)
MODEL_APIS = (
    MODEL_API_OPENAI_CHAT_COMPLETIONS,
    MODEL_API_OPENAI_RESPONSES,
    MODEL_API_ANTHROPIC_MESSAGES,
)
VOICE_PRESETS = ("none", VOICE_PROVIDER_SONIOX, VOICE_PROVIDER_QWEN, VOICE_PROVIDER_CUSTOM)
VOICE_NESTED_PROVIDERS = (VOICE_PROVIDER_SONIOX, VOICE_PROVIDER_QWEN)
MODEL_PLACEHOLDER = "your_model_here"
API_KEY_PLACEHOLDER = "your_api_key_here"


@dataclass(frozen=True)
class ConfigChange:
    """One user-visible config mutation."""

    path: str
    before: str
    after: str


@dataclass(frozen=True)
class ConfigUpdate:
    """Prepared config update with the edited document and display diff."""

    data: dict[str, Any]
    changes: tuple[ConfigChange, ...]


def config_path(config_dir: Path) -> Path:
    return config_dir / BaseAgentConfig.CONFIG_FILENAME


def load_config(config_dir: Path) -> dict[str, Any]:
    path = config_path(config_dir)
    if not path.is_file():
        raise FileNotFoundError(f"Config not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Configuration must be a mapping: {path}")
    return data


def validate_config(data: dict[str, Any]) -> None:
    runner = BaseAgentRunner.__new__(BaseAgentRunner)
    runner._validate_config(data)


def write_config(config_dir: Path, data: dict[str, Any]) -> None:
    validate_config(data)
    path = config_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = yaml.safe_dump(data, sort_keys=False, allow_unicode=False)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(payload, encoding="utf-8")
    os.replace(tmp_path, path)


def _clone_config(config: dict[str, Any]) -> dict[str, Any]:
    payload = yaml.safe_dump(config, sort_keys=False, allow_unicode=False)
    data = yaml.safe_load(payload) or {}
    return data if isinstance(data, dict) else {}


def _display(value: Any) -> str:
    if value is None:
        return "(unset)"
    if isinstance(value, str):
        if is_placeholder_api_key(value):
            return "(missing)"
        if "api_key" in value.lower():
            return "(secret)"
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _secret_display(value: Any) -> str:
    raw = str(value or "").strip()
    if is_placeholder_api_key(raw):
        return "(missing)"
    return "(secret)"


def _get_path(data: dict[str, Any], dotted_path: str) -> Any:
    current: Any = data
    for part in dotted_path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _path_uses_secret_display(path: str) -> bool:
    return path.rsplit(".", 1)[-1] in {"api_key", "secret_key", "app_secret"}


def _changed(before: dict[str, Any], after: dict[str, Any], paths: tuple[str, ...]) -> tuple[ConfigChange, ...]:
    changes: list[ConfigChange] = []
    for path in paths:
        old = _get_path(before, path)
        new = _get_path(after, path)
        if old == new:
            continue
        old_display = _secret_display(old) if _path_uses_secret_display(path) else _display(old)
        new_display = _secret_display(new) if _path_uses_secret_display(path) else _display(new)
        changes.append(ConfigChange(path=path, before=old_display, after=new_display))
    return tuple(changes)


def prepare_update(config: dict[str, Any], mutator: Callable[[dict[str, Any]], None], paths: tuple[str, ...]) -> ConfigUpdate:
    before = _clone_config(config)
    after = _clone_config(config)
    mutator(after)
    validate_config(after)
    return ConfigUpdate(data=after, changes=_changed(before, after, paths))


def provider_needs_feature_key(config: dict[str, Any], provider: str) -> bool:
    if provider == SEARCH_PROVIDER_BUILTIN:
        return False
    model_provider = normalize_provider_name((config.get("provider") or {}).get("name"))
    return provider != model_provider


def image_generation_provider_needs_feature_key(config: dict[str, Any], provider: str) -> bool:
    model_provider = normalize_provider_name((config.get("provider") or {}).get("name"))
    return provider != IMAGE_GENERATION_PROVIDER_NONE and provider != model_provider


def _image_generation_defaults(provider: str) -> dict[str, Any]:
    if provider == IMAGE_GENERATION_PROVIDER_OPENAI:
        return {
            "provider": provider,
            "model": DEFAULT_IMAGE_GENERATION_MODEL,
            "size": DEFAULT_IMAGE_GENERATION_SIZE,
            "quality": DEFAULT_IMAGE_GENERATION_QUALITY,
        }
    if provider == IMAGE_GENERATION_PROVIDER_MINIMAX:
        return {
            "provider": provider,
            "model": DEFAULT_MINIMAX_IMAGE_GENERATION_MODEL,
            "aspect_ratio": DEFAULT_MINIMAX_IMAGE_GENERATION_ASPECT_RATIO,
        }
    if provider == IMAGE_GENERATION_PROVIDER_QWEN:
        return {
            "provider": provider,
            "model": DEFAULT_QWEN_IMAGE_GENERATION_MODEL,
            "size": DEFAULT_QWEN_IMAGE_GENERATION_SIZE,
        }
    return {"provider": IMAGE_GENERATION_PROVIDER_NONE}


def _existing_feature_key(config: dict[str, Any], section: str, provider: str) -> str | None:
    current = config.get(section)
    if not isinstance(current, dict):
        return None
    current_provider = normalize_provider_name(current.get("provider"))
    api_key = str(current.get("api_key") or "").strip()
    if current_provider == provider and api_key and not is_placeholder_api_key(api_key):
        return api_key
    return None


def _provider_api_key(provider_cfg: Any, provider: str) -> str | None:
    if not isinstance(provider_cfg, dict):
        return None
    if normalize_provider_name(provider_cfg.get("name")) != provider:
        return None
    api_key = str(provider_cfg.get("api_key") or "").strip()
    if api_key and not is_placeholder_api_key(api_key):
        return api_key
    return None


def _resolve_feature_api_key(
    config: dict[str, Any],
    *,
    section: str,
    provider: str,
    explicit_api_key: str | None = None,
    provider_fallbacks: tuple[Any, ...] = (),
) -> str:
    configured_key = str(explicit_api_key or "").strip()
    if configured_key:
        return configured_key

    existing_key = _existing_feature_key(config, section, provider)
    if existing_key:
        return existing_key

    for provider_cfg in provider_fallbacks:
        provider_key = _provider_api_key(provider_cfg, provider)
        if provider_key:
            return provider_key

    return API_KEY_PLACEHOLDER


def prepare_model_provider_update(
    config: dict[str, Any],
    *,
    provider: str,
    model: str,
    api_key: str | None = None,
    base_url: str | None = None,
    model_api: str | None = None,
    supports_vision: bool | None = None,
    search_api_key: str | None = None,
    image_generation_api_key: str | None = None,
) -> ConfigUpdate:
    normalized_provider = normalize_provider_name(provider)
    if normalized_provider not in KNOWN_PROVIDERS:
        allowed = ", ".join(KNOWN_PROVIDERS)
        raise ValueError(f"Unsupported model provider: {provider}. Expected one of: {allowed}")

    selected_model = model.strip() or MODEL_PLACEHOLDER
    selected_model_api = (
        normalize_model_api(model_api or MODEL_API_OPENAI_CHAT_COMPLETIONS)
        if normalized_provider == PROVIDER_CUSTOM
        else None
    )

    def mutate(data: dict[str, Any]) -> None:
        current_provider = data.get("provider") if isinstance(data.get("provider"), dict) else {}
        provider_config: dict[str, Any] = {
            "name": normalized_provider,
            "base_url": (base_url or provider_base_url(normalized_provider, selected_model_api)).strip(),
            "api_key": (api_key or "").strip()
            or (
                str(current_provider.get("api_key") or "").strip()
                if normalize_provider_name(current_provider.get("name")) == normalized_provider
                else API_KEY_PLACEHOLDER
            ),
            "model": selected_model,
        }
        if "max_tokens" in current_provider:
            provider_config["max_tokens"] = current_provider["max_tokens"]
        if normalized_provider == PROVIDER_CUSTOM:
            provider_config["model_api"] = selected_model_api or MODEL_API_OPENAI_CHAT_COMPLETIONS
            provider_config["supports_vision"] = bool(supports_vision)

        search = data.get("search")
        if isinstance(search, dict):
            search_provider = normalize_search_provider(search.get("provider"))
            if search_provider != SEARCH_PROVIDER_BUILTIN:
                feature_key = _resolve_feature_api_key(
                    data,
                    section="search",
                    provider=search_provider,
                    explicit_api_key=search_api_key,
                    provider_fallbacks=(provider_config, current_provider),
                )
                search["api_key"] = feature_key

        image_generation = data.get("image_generation")
        if isinstance(image_generation, dict):
            image_provider = normalize_image_generation_provider(image_generation.get("provider"))
            if image_provider != IMAGE_GENERATION_PROVIDER_NONE:
                feature_key = _resolve_feature_api_key(
                    data,
                    section="image_generation",
                    provider=image_provider,
                    explicit_api_key=image_generation_api_key,
                    provider_fallbacks=(provider_config, current_provider),
                )
                image_generation["api_key"] = feature_key

        data["provider"] = provider_config

    return prepare_update(
        config,
        mutate,
        (
            "provider.name",
            "provider.model_api",
            "provider.base_url",
            "provider.api_key",
            "provider.model",
            "provider.supports_vision",
            "search.api_key",
            "image_generation.api_key",
        ),
    )


def prepare_search_provider_update(
    config: dict[str, Any],
    *,
    provider: str,
    api_key: str | None = None,
) -> ConfigUpdate:
    normalized_provider = normalize_search_provider(provider)
    if normalized_provider not in SEARCH_PROVIDERS:
        raise ValueError(f"Unsupported search provider: {provider}")

    def mutate(data: dict[str, Any]) -> None:
        search = {"provider": normalized_provider}
        if normalized_provider != SEARCH_PROVIDER_BUILTIN:
            current_provider = data.get("provider") if isinstance(data.get("provider"), dict) else {}
            search["api_key"] = _resolve_feature_api_key(
                data,
                section="search",
                provider=normalized_provider,
                explicit_api_key=api_key,
                provider_fallbacks=(current_provider,),
            )
        data["search"] = search

    return prepare_update(config, mutate, ("search.provider", "search.api_key"))


def prepare_image_generation_provider_update(
    config: dict[str, Any],
    *,
    provider: str,
    api_key: str | None = None,
) -> ConfigUpdate:
    normalized_provider = normalize_image_generation_provider(provider)
    if normalized_provider not in IMAGE_GENERATION_PROVIDERS:
        raise ValueError(f"Unsupported image generation provider: {provider}")

    def mutate(data: dict[str, Any]) -> None:
        current = data.get("image_generation") if isinstance(data.get("image_generation"), dict) else {}
        if normalize_image_generation_provider(current.get("provider")) == normalized_provider:
            image_generation = dict(current)
            image_generation["provider"] = normalized_provider
        else:
            image_generation = _image_generation_defaults(normalized_provider)
        if normalized_provider != IMAGE_GENERATION_PROVIDER_NONE:
            current_provider = data.get("provider") if isinstance(data.get("provider"), dict) else {}
            image_generation["api_key"] = _resolve_feature_api_key(
                data,
                section="image_generation",
                provider=normalized_provider,
                explicit_api_key=api_key,
                provider_fallbacks=(current_provider,),
            )
        data["image_generation"] = image_generation

    return prepare_update(
        config,
        mutate,
        (
            "image_generation.provider",
            "image_generation.api_key",
            "image_generation.model",
            "image_generation.size",
            "image_generation.quality",
            "image_generation.aspect_ratio",
        ),
    )


def prepare_observability_update(
    config: dict[str, Any],
    *,
    enabled: bool,
    public_key: str | None = None,
    secret_key: str | None = None,
    base_url: str | None = None,
) -> ConfigUpdate:
    provider_cfg = config.get("provider") if isinstance(config.get("provider"), dict) else {}
    if enabled and not model_api_uses_openai_client(provider_model_api(provider_cfg)):
        raise ValueError("Observability requires an OpenAI-compatible model API")

    def mutate(data: dict[str, Any]) -> None:
        current = data.get("observability") if isinstance(data.get("observability"), dict) else {}
        if not enabled:
            if not current:
                data.pop("observability", None)
                return
            observability = dict(current)
            observability["enabled"] = False
            data["observability"] = observability
            return

        observability = dict(current)
        observability["enabled"] = True
        observability["provider"] = "langfuse"

        resolved_public_key = (public_key or "").strip() or str(current.get("public_key") or "").strip()
        resolved_secret_key = (secret_key or "").strip() or str(current.get("secret_key") or "").strip()
        resolved_base_url = (base_url or "").strip() or str(current.get("base_url") or "").strip()

        observability["public_key"] = resolved_public_key
        observability["secret_key"] = resolved_secret_key
        if resolved_base_url:
            observability["base_url"] = resolved_base_url
        else:
            observability.pop("base_url", None)
        data["observability"] = observability

    return prepare_update(
        config,
        mutate,
        (
            "observability.enabled",
            "observability.provider",
            "observability.public_key",
            "observability.secret_key",
            "observability.base_url",
        ),
    )


def _voice_key_placeholder(provider: str) -> str:
    return QWEN_KEY_PLACEHOLDER if provider == VOICE_PROVIDER_QWEN else SONIOX_KEY_PLACEHOLDER


def _voice_stt(provider: str, api_key: str | None = None) -> dict[str, Any]:
    config = {
        "provider": provider,
        "api_key": (api_key or "").strip() or _voice_key_placeholder(provider),
    }
    if provider == VOICE_PROVIDER_QWEN:
        config.update(
            {
                "model": "qwen3-asr-flash-realtime",
                "audio_format": "pcm",
                "vad_threshold": 0.2,
                "silence_duration_ms": 400,
            }
        )
    else:
        config.update({"model": "stt-rt-v4", "audio_format": "pcm_s16le"})
    return config


def _voice_tts(provider: str, api_key: str | None = None) -> dict[str, Any]:
    config = {
        "provider": provider,
        "api_key": (api_key or "").strip() or _voice_key_placeholder(provider),
    }
    if provider == VOICE_PROVIDER_QWEN:
        config.update(
            {
                "model": "qwen3-tts-flash-realtime",
                "voice": "Cherry",
                "audio_format": "pcm",
            }
        )
    else:
        config.update(
            {
                "model": "tts-rt-v1",
                "voice": "Owen",
                "audio_format": "pcm_s16le",
            }
        )
    return config


def _voice_base(provider: str) -> dict[str, Any]:
    return {
        "provider": provider,
        "enable_interruptions": False,
        "audio": {
            "input": "auto",
            "output": "auto",
        },
        "wake": {
            "enabled": False,
            "wake_phrases": ["xAgent"],
            "exit_phrases": ["exit", "stop", "goodbye", "that's all", "never mind"],
            "match_mode": "prefix",
            "idle_timeout_seconds": 60,
        },
    }


def _current_voice(config: dict[str, Any]) -> dict[str, Any]:
    channels = config.get("channels")
    if not isinstance(channels, dict):
        return {}
    voice = channels.get("voice")
    return dict(voice) if isinstance(voice, dict) else {}


def _current_nested_provider(voice: dict[str, Any], section: str, default: str = VOICE_PROVIDER_SONIOX) -> str:
    nested = voice.get(section)
    if isinstance(nested, dict):
        provider = str(nested.get("provider") or "").strip().lower()
        if provider in VOICE_NESTED_PROVIDERS:
            return provider
    provider = str(voice.get("provider") or "").strip().lower()
    return provider if provider in VOICE_NESTED_PROVIDERS else default


def _current_nested_api_key(voice: dict[str, Any], section: str, provider: str) -> str | None:
    nested = voice.get(section)
    if not isinstance(nested, dict):
        return None
    nested_provider = str(nested.get("provider") or voice.get("provider") or "").strip().lower()
    api_key = str(nested.get("api_key") or "").strip()
    if nested_provider == provider and api_key and not is_placeholder_api_key(api_key):
        return api_key
    return None


def prepare_voice_preset_update(
    config: dict[str, Any],
    *,
    provider: str,
    api_key: str | None = None,
) -> ConfigUpdate:
    normalized_provider = provider.strip().lower()
    if normalized_provider not in VOICE_PRESETS:
        raise ValueError(f"Unsupported voice provider: {provider}")

    def mutate(data: dict[str, Any]) -> None:
        channels = data.setdefault("channels", {})
        if not isinstance(channels, dict):
            raise ValueError("channels must be a dictionary")
        if normalized_provider == "none":
            channels.pop("voice", None)
            return
        if normalized_provider == VOICE_PROVIDER_CUSTOM:
            current = _current_voice(data)
            stt_provider = _current_nested_provider(current, "stt")
            tts_provider = _current_nested_provider(current, "tts", default=VOICE_PROVIDER_QWEN)
            voice = _voice_base(VOICE_PROVIDER_CUSTOM)
            voice["stt"] = _voice_stt(stt_provider, _current_nested_api_key(current, "stt", stt_provider))
            voice["tts"] = _voice_tts(tts_provider, _current_nested_api_key(current, "tts", tts_provider))
            channels["voice"] = voice
            return
        voice = _voice_base(normalized_provider)
        current = _current_voice(data)
        existing_key = (
            _current_nested_api_key(current, "stt", normalized_provider)
            or _current_nested_api_key(current, "tts", normalized_provider)
        )
        resolved_api_key = api_key or existing_key
        voice["stt"] = _voice_stt(normalized_provider, resolved_api_key)
        voice["tts"] = _voice_tts(normalized_provider, resolved_api_key)
        channels["voice"] = voice

    return prepare_update(
        config,
        mutate,
        (
            "channels.voice.provider",
            "channels.voice.stt.provider",
            "channels.voice.stt.api_key",
            "channels.voice.stt.model",
            "channels.voice.tts.provider",
            "channels.voice.tts.api_key",
            "channels.voice.tts.model",
            "channels.voice.tts.voice",
        ),
    )


def prepare_voice_nested_provider_update(
    config: dict[str, Any],
    *,
    section: str,
    provider: str,
    api_key: str | None = None,
) -> ConfigUpdate:
    if section not in {"stt", "tts"}:
        raise ValueError("Voice section must be stt or tts")
    normalized_provider = provider.strip().lower()
    if normalized_provider not in VOICE_NESTED_PROVIDERS:
        raise ValueError(f"Unsupported voice provider: {provider}")

    def mutate(data: dict[str, Any]) -> None:
        channels = data.setdefault("channels", {})
        if not isinstance(channels, dict):
            raise ValueError("channels must be a dictionary")
        current = _current_voice(data)
        stt_provider = _current_nested_provider(current, "stt")
        tts_provider = _current_nested_provider(current, "tts", default=VOICE_PROVIDER_QWEN)
        if section == "stt":
            stt_provider = normalized_provider
        else:
            tts_provider = normalized_provider
        voice = _voice_base(VOICE_PROVIDER_CUSTOM)
        stt_key = (
            api_key
            if section == "stt"
            else _current_nested_api_key(current, "stt", stt_provider)
        )
        tts_key = (
            api_key
            if section == "tts"
            else _current_nested_api_key(current, "tts", tts_provider)
        )
        voice["stt"] = _voice_stt(stt_provider, stt_key)
        voice["tts"] = _voice_tts(tts_provider, tts_key)
        channels["voice"] = voice

    return prepare_update(
        config,
        mutate,
        (
            "channels.voice.provider",
            f"channels.voice.{section}.provider",
            f"channels.voice.{section}.api_key",
            f"channels.voice.{section}.model",
            "channels.voice.tts.voice",
        ),
    )


def _require_voice(data: dict[str, Any]) -> dict[str, Any]:
    channels = data.setdefault("channels", {})
    if not isinstance(channels, dict):
        raise ValueError("channels must be a dictionary")
    voice = channels.get("voice")
    if not isinstance(voice, dict):
        raise ValueError("channels.voice must be configured before updating voice options")
    return voice


def prepare_voice_interruptions_update(config: dict[str, Any], *, enabled: bool) -> ConfigUpdate:
    def mutate(data: dict[str, Any]) -> None:
        voice = _require_voice(data)
        voice["enable_interruptions"] = bool(enabled)

    return prepare_update(config, mutate, ("channels.voice.enable_interruptions",))


def prepare_voice_wake_update(
    config: dict[str, Any],
    *,
    enabled: bool | None = None,
    wake_phrases: list[str] | None = None,
    exit_phrases: list[str] | None = None,
    match_mode: str | None = None,
    idle_timeout_seconds: float | None = None,
) -> ConfigUpdate:
    def mutate(data: dict[str, Any]) -> None:
        voice = _require_voice(data)
        wake = voice.setdefault("wake", {})
        if not isinstance(wake, dict):
            raise ValueError("channels.voice.wake must be a dictionary")
        if enabled is not None:
            wake["enabled"] = bool(enabled)
        if wake_phrases is not None:
            wake["wake_phrases"] = [item.strip() for item in wake_phrases if item.strip()]
        if exit_phrases is not None:
            wake["exit_phrases"] = [item.strip() for item in exit_phrases if item.strip()]
        if match_mode is not None:
            wake["match_mode"] = match_mode.strip().lower()
        if idle_timeout_seconds is not None:
            wake["idle_timeout_seconds"] = float(idle_timeout_seconds)

    return prepare_update(
        config,
        mutate,
        (
            "channels.voice.wake.enabled",
            "channels.voice.wake.wake_phrases",
            "channels.voice.wake.exit_phrases",
            "channels.voice.wake.match_mode",
            "channels.voice.wake.idle_timeout_seconds",
        ),
    )


def validate_voice_config(data: dict[str, Any]) -> None:
    channels = data.get("channels")
    if not isinstance(channels, dict) or "voice" not in channels:
        return
    VoiceChannelConfig.from_dict(channels["voice"])
