"""Configuration for the Soniox-only local voice channel."""
from __future__ import annotations

import os
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

SONIOX_KEY_PLACEHOLDER = "your_soniox_api_key_here"
SONIOX_STT_MODEL = "stt-rt-v5"
SONIOX_STT_SAMPLE_RATE = 16_000
SONIOX_STT_CHANNELS = 1
SONIOX_TTS_MODEL = "tts-rt-v1"
SONIOX_TTS_SAMPLE_RATE = 24_000
SONIOX_TTS_CHANNELS = 1
SONIOX_AUDIO_FORMAT = "pcm_s16le"
SONIOX_ENDPOINT_LATENCY_LEVEL = 2
SONIOX_ENDPOINT_SENSITIVITY = 0.3
SONIOX_MAX_ENDPOINT_DELAY_MS = 1_500
SONIOX_TTS_MAX_TEXT_CHARS = 5_000

_VOICE_KEY_PLACEHOLDERS = {
    SONIOX_KEY_PLACEHOLDER,
    "your_qwen_api_key_here",
    "your_api_key_here",
}
_LEGACY_VOICE_KEYS = {
    "enabled",
    "provider",
    "stt",
    "tts",
    "websocket_base_url",
    "enable_interruptions",
    "wake",
    "return_timestamps",
}

VOICE_CONFIG_EXAMPLE = """channels:
  voice:
    api_key: your_soniox_api_key_here
    voice: Owen
    language_hints: [zh, en]
    fallback_language: zh
    speed: 1.0
    context:
      general: []
      text:
      terms: []
    audio:
      input: auto
      output: auto"""


def _migration_error(keys: set[str]) -> ValueError:
    fields = ", ".join(sorted(keys))
    return ValueError(
        "Legacy or Qwen voice configuration is no longer supported "
        f"(found: {fields}). Voice is now Soniox-only and uses a flat configuration. "
        "Replace channels.voice with:\n\n"
        f"{VOICE_CONFIG_EXAMPLE}"
    )


class SonioxSTTContextConfig(BaseModel):
    """Structured context passed directly to Soniox realtime STT."""

    model_config = ConfigDict(extra="forbid")

    general: list[dict[str, str]] = Field(default_factory=list)
    text: str | None = None
    terms: list[str] = Field(default_factory=list)

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("general")
    @classmethod
    def _validate_general(cls, value: list[dict[str, str]]) -> list[dict[str, str]]:
        cleaned: list[dict[str, str]] = []
        for item in value:
            key = str(item.get("key") or "").strip()
            item_value = str(item.get("value") or "").strip()
            if not key or not item_value:
                raise ValueError("voice.context.general entries require non-empty key and value")
            cleaned.append({"key": key, "value": item_value})
        return cleaned

    @field_validator("terms")
    @classmethod
    def _validate_terms(cls, value: list[str]) -> list[str]:
        return [term.strip() for term in value if term.strip()]

    def to_soniox_payload(self) -> dict[str, Any] | None:
        payload: dict[str, Any] = {}
        if self.general:
            payload["general"] = list(self.general)
        if self.text:
            payload["text"] = self.text
        if self.terms:
            payload["terms"] = list(self.terms)
        return payload or None


class VoiceAudioConfig(BaseModel):
    """Local audio-device preferences."""

    model_config = ConfigDict(extra="forbid")

    input: str | int | None = "auto"
    output: str | int | None = "auto"

    @field_validator("input", "output")
    @classmethod
    def _validate_device_preference(cls, value: str | int | None) -> str | int | None:
        if value is None:
            return None
        if isinstance(value, int):
            if value < 0:
                raise ValueError("voice.audio device index must be non-negative")
            return value
        return value.strip() or "auto"


class VoiceChannelConfig(BaseModel):
    """Flat user-facing configuration for ``channels.voice``."""

    model_config = ConfigDict(extra="forbid")

    api_key: str | None = None
    voice: str = "Owen"
    language_hints: list[str] = Field(default_factory=lambda: ["zh", "en"])
    fallback_language: str = "zh"
    speed: float = Field(default=1.0, ge=0.7, le=1.3)
    context: SonioxSTTContextConfig = Field(default_factory=SonioxSTTContextConfig)
    audio: VoiceAudioConfig = Field(default_factory=VoiceAudioConfig)

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_configuration(cls, value: Any) -> Any:
        if isinstance(value, dict):
            legacy_keys = _LEGACY_VOICE_KEYS.intersection(value)
            if legacy_keys:
                raise _migration_error(legacy_keys)
        return value

    @field_validator("api_key")
    @classmethod
    def _validate_api_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("voice", "fallback_language")
    @classmethod
    def _validate_non_empty(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("voice and fallback_language must be non-empty")
        return normalized

    @field_validator("language_hints")
    @classmethod
    def _validate_language_hints(cls, value: list[str]) -> list[str]:
        hints = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        if not hints:
            raise ValueError("voice.language_hints must include at least one language")
        return hints

    @classmethod
    def from_dict(cls, data: Any) -> "VoiceChannelConfig":
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ValueError("channels.voice must be a dictionary")
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            raise ValueError(str(exc)) from exc

    def resolved_api_key(self) -> str:
        configured = str(self.api_key or "").strip()
        if configured and configured not in _VOICE_KEY_PLACEHOLDERS:
            return configured
        environment = os.getenv("SONIOX_API_KEY", "").strip()
        if environment and environment not in _VOICE_KEY_PLACEHOLDERS:
            return environment
        raise ValueError(
            "Soniox voice API key is required. Set channels.voice.api_key in config.yaml "
            "or the SONIOX_API_KEY environment variable."
        )

    def tts_language_for(self, stt_language: str | None) -> str:
        return (stt_language or "").strip() or self.fallback_language
