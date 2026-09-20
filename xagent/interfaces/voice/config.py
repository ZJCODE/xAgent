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
SONIOX_TTS_MODEL = "tts-rt-v2"
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


class VoiceAttentionConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    open_window_seconds: float = Field(default=25.0, ge=0.0, le=120.0)
    wake_terms: list[str] = Field(default_factory=list)
    use_decide_participation: bool = True

    @field_validator("wake_terms")
    @classmethod
    def _clean_terms(cls, value: list[str]) -> list[str]:
        return [term.strip() for term in value if term.strip()]


class VoicePresenceConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    close_stt_after_idle_seconds: float = Field(default=0.0, ge=0.0, le=86_400.0)
    wake_energy_rms: float = Field(default=450.0, ge=50.0, le=20_000.0)
    recent_speech_hours: float = Field(default=6.0, ge=0.0, le=168.0)


class VoiceProactiveConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quiet_hours_start: int = Field(default=22, ge=0, le=23)
    quiet_hours_end: int = Field(default=7, ge=0, le=23)
    max_per_hour: int = Field(default=3, ge=0, le=30)
    require_recent_speech: bool = True


class VoiceInterruptionConfig(BaseModel):
    """Barge-in thresholds when ``enable_interruptions`` is true."""

    model_config = ConfigDict(extra="forbid")

    min_speech_ms: int = Field(default=250, ge=50, le=2_000)
    min_words: int = Field(default=2, ge=1, le=8)


class VoiceAudioConfig(BaseModel):
    """Local audio-device preferences."""

    model_config = ConfigDict(extra="forbid")

    input: str | int | None = "auto"
    output: str | int | None = "auto"
    echo_cancellation: str = Field(default="none")

    @field_validator("echo_cancellation")
    @classmethod
    def _validate_echo_cancellation(cls, value: str) -> str:
        normalized = (value or "none").strip().lower()
        if normalized not in {"none", "device", "software"}:
            raise ValueError("voice.audio.echo_cancellation must be one of: none, device, software")
        return normalized

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
    return_timestamps: bool = True
    enable_interruptions: bool = False
    interruption: VoiceInterruptionConfig = Field(default_factory=VoiceInterruptionConfig)
    aggregate_utterances: bool = True
    enable_diarization: bool = True
    room_name: str = "local_room"
    attention: VoiceAttentionConfigModel = Field(default_factory=VoiceAttentionConfigModel)
    presence: VoicePresenceConfigModel = Field(default_factory=VoicePresenceConfigModel)
    proactive: VoiceProactiveConfigModel = Field(default_factory=VoiceProactiveConfigModel)
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
        """Deprecated: prefer reply-based ``ConversationLanguageTracker``."""
        return (stt_language or "").strip() or self.fallback_language

    def merged_stt_context(self, extra_terms: list[str] | None = None) -> SonioxSTTContextConfig:
        terms = list(self.context.terms)
        for term in extra_terms or []:
            cleaned = term.strip()
            if cleaned and cleaned not in terms:
                terms.append(cleaned)
        for term in self.attention.wake_terms:
            if term not in terms:
                terms.append(term)
        if terms == self.context.terms:
            return self.context
        return SonioxSTTContextConfig(
            general=list(self.context.general),
            text=self.context.text,
            terms=terms,
        )
