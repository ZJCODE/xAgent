"""Intent-focused config updates for the launcher."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

from ..core.providers import PROVIDER_MINIMAX, PROVIDER_OPENAI, PROVIDER_QWEN, normalize_provider_name
from ..tools.search_tool import is_placeholder_api_key, normalize_search_provider
from ..voice.config import (
    QWEN_KEY_PLACEHOLDER,
    SONIOX_KEY_PLACEHOLDER,
    VOICE_PROVIDER_CUSTOM,
    VOICE_PROVIDER_QWEN,
    VOICE_PROVIDER_SONIOX,
    VoiceChannelConfig,
)
from .base import BaseAgentConfig, BaseAgentRunner


SEARCH_PROVIDER_NONE = "none"
SEARCH_PROVIDERS = (SEARCH_PROVIDER_NONE, PROVIDER_OPENAI, PROVIDER_QWEN, PROVIDER_MINIMAX)
VOICE_PRESETS = ("none", VOICE_PROVIDER_SONIOX, VOICE_PROVIDER_QWEN, VOICE_PROVIDER_CUSTOM)
VOICE_NESTED_PROVIDERS = (VOICE_PROVIDER_SONIOX, VOICE_PROVIDER_QWEN)


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


def _changed(before: dict[str, Any], after: dict[str, Any], paths: tuple[str, ...]) -> tuple[ConfigChange, ...]:
    changes: list[ConfigChange] = []
    for path in paths:
        old = _get_path(before, path)
        new = _get_path(after, path)
        if old == new:
            continue
        old_display = _secret_display(old) if path.endswith("api_key") else _display(old)
        new_display = _secret_display(new) if path.endswith("api_key") else _display(new)
        changes.append(ConfigChange(path=path, before=old_display, after=new_display))
    return tuple(changes)


def prepare_update(config: dict[str, Any], mutator: Callable[[dict[str, Any]], None], paths: tuple[str, ...]) -> ConfigUpdate:
    before = _clone_config(config)
    after = _clone_config(config)
    mutator(after)
    validate_config(after)
    return ConfigUpdate(data=after, changes=_changed(before, after, paths))


def provider_needs_feature_key(config: dict[str, Any], provider: str) -> bool:
    model_provider = normalize_provider_name((config.get("provider") or {}).get("name"))
    return provider != SEARCH_PROVIDER_NONE and provider != model_provider


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
        if normalized_provider != SEARCH_PROVIDER_NONE:
            if provider_needs_feature_key(data, normalized_provider):
                search["api_key"] = (api_key or "").strip()
            elif api_key:
                search["api_key"] = api_key.strip()
        data["search"] = search

    return prepare_update(config, mutate, ("search.provider", "search.api_key"))


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


def validate_voice_config(data: dict[str, Any]) -> None:
    channels = data.get("channels")
    if not isinstance(channels, dict) or "voice" not in channels:
        return
    VoiceChannelConfig.from_dict(channels["voice"])
