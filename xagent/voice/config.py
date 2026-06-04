"""Configuration models for the local voice channel."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


VOICE_PROVIDER_SONIOX = "soniox"
VOICE_PROVIDER_QWEN = "qwen"
VOICE_PROVIDERS = {VOICE_PROVIDER_SONIOX, VOICE_PROVIDER_QWEN}
SONIOX_KEY_PLACEHOLDER = "your_soniox_api_key_here"
QWEN_KEY_PLACEHOLDER = "your_qwen_api_key_here"
GENERIC_KEY_PLACEHOLDER = "your_api_key_here"
_VALID_TTS_SAMPLE_RATES = {8000, 16000, 24000, 44100, 48000}
_VALID_STT_AUDIO_FORMATS = {"pcm", "pcm_s16le"}
_VALID_TTS_AUDIO_FORMATS = {"pcm", "pcm_s16le"}
_QWEN_VAD_THRESHOLD_DEFAULT = 0.2
_QWEN_SILENCE_DURATION_MS_DEFAULT = 400
_VOICE_KEY_PLACEHOLDERS = {
    SONIOX_KEY_PLACEHOLDER,
    QWEN_KEY_PLACEHOLDER,
    GENERIC_KEY_PLACEHOLDER,
}

_DEFAULT_STT_MODELS = {
    VOICE_PROVIDER_SONIOX: "stt-rt-v4",
    VOICE_PROVIDER_QWEN: "qwen3-asr-flash-realtime",
}
_DEFAULT_TTS_MODELS = {
    VOICE_PROVIDER_SONIOX: "tts-rt-v1",
    VOICE_PROVIDER_QWEN: "qwen3-tts-flash-realtime",
}
_DEFAULT_TTS_VOICES = {
    VOICE_PROVIDER_SONIOX: "Owen",
    VOICE_PROVIDER_QWEN: "Cherry",
}


def _channel_voice_provider(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized or normalized == "none":
        return None
    if normalized not in VOICE_PROVIDERS:
        allowed = ", ".join(sorted(VOICE_PROVIDERS))
        raise ValueError(f"voice provider must be one of: {allowed}")
    return normalized


def _voice_provider(value: str | None) -> str:
    normalized = _channel_voice_provider(value)
    if normalized is None:
        return VOICE_PROVIDER_SONIOX
    return normalized


def _normalize_provider_audio_format(provider: str, value: str) -> str:
    normalized = value.strip().lower()
    if provider == VOICE_PROVIDER_QWEN and normalized == "pcm_s16le":
        return "pcm"
    if provider == VOICE_PROVIDER_SONIOX and normalized == "pcm":
        return "pcm_s16le"
    return normalized


class VoiceSTTConfig(BaseModel):
    """Realtime speech-to-text configuration."""

    model_config = ConfigDict(extra="forbid")

    provider: str = VOICE_PROVIDER_SONIOX
    model: str = "stt-rt-v4"
    audio_format: str = "pcm_s16le"
    sample_rate: int = 16000
    num_channels: int = 1
    enable_endpoint_detection: bool = True
    max_endpoint_delay_ms: int = Field(default=700, ge=500, le=3000)
    language_hints: list[str] = Field(default_factory=lambda: ["zh", "en"])
    enable_language_identification: bool = True
    enable_speaker_diarization: bool = False
    language: str = "zh"
    turn_detection: Literal["server_vad"] = "server_vad"
    silence_duration_ms: int = Field(default=_QWEN_SILENCE_DURATION_MS_DEFAULT, ge=200, le=3000)
    vad_threshold: float = Field(default=_QWEN_VAD_THRESHOLD_DEFAULT, ge=0.0, le=1.0)
    session_options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("provider")
    @classmethod
    def _validate_provider(cls, value: str) -> str:
        return _voice_provider(value)

    @field_validator("model")
    @classmethod
    def _validate_model(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("voice.stt.model must be non-empty")
        return normalized

    @field_validator("audio_format")
    @classmethod
    def _validate_audio_format(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in _VALID_STT_AUDIO_FORMATS:
            raise ValueError("voice.stt.audio_format must be one of: pcm, pcm_s16le")
        return normalized

    @field_validator("sample_rate")
    @classmethod
    def _validate_sample_rate(cls, value: int) -> int:
        if value != 16000:
            raise ValueError("voice.stt.sample_rate must be 16000")
        return value

    @field_validator("num_channels")
    @classmethod
    def _validate_num_channels(cls, value: int) -> int:
        if value != 1:
            raise ValueError("voice.stt.num_channels must be 1")
        return value

    @field_validator("language_hints")
    @classmethod
    def _validate_language_hints(cls, value: list[str]) -> list[str]:
        hints = [item.strip() for item in value if item.strip()]
        if not hints:
            raise ValueError("voice.stt.language_hints must include at least one language")
        return hints

    @field_validator("session_options")
    @classmethod
    def _validate_session_options(cls, value: dict[str, Any]) -> dict[str, Any]:
        return dict(value)

    @model_validator(mode="after")
    def _validate_endpointing(self) -> "VoiceSTTConfig":
        self.audio_format = _normalize_provider_audio_format(self.provider, self.audio_format)
        if self.provider == VOICE_PROVIDER_SONIOX and not self.enable_endpoint_detection:
            raise ValueError("voice.stt.enable_endpoint_detection must be true")
        return self


class VoiceTTSConfig(BaseModel):
    """Realtime text-to-speech configuration."""

    model_config = ConfigDict(extra="forbid")

    provider: str = VOICE_PROVIDER_SONIOX
    model: str = "tts-rt-v1"
    voice: str = "Owen"
    audio_format: str = "pcm_s16le"
    sample_rate: int = 24000
    language_policy: Literal["from_stt_dominant", "fallback"] = "from_stt_dominant"
    fallback_language: str = "zh"
    max_buffer_chars: int = Field(default=80, ge=1, le=500)
    mode: Literal["server_commit", "commit"] = "server_commit"
    language_type: str = "Auto"
    instructions: str | None = None
    optimize_instructions: bool = False
    session_options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("provider")
    @classmethod
    def _validate_provider(cls, value: str) -> str:
        return _voice_provider(value)

    @field_validator("model", "voice", "fallback_language", "language_type")
    @classmethod
    def _validate_non_empty(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("voice.tts string fields must be non-empty")
        return normalized

    @field_validator("instructions")
    @classmethod
    def _validate_instructions(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("session_options")
    @classmethod
    def _validate_session_options(cls, value: dict[str, Any]) -> dict[str, Any]:
        return dict(value)

    @field_validator("audio_format")
    @classmethod
    def _validate_audio_format(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in _VALID_TTS_AUDIO_FORMATS:
            raise ValueError("voice.tts.audio_format must be one of: pcm, pcm_s16le")
        return normalized

    @field_validator("sample_rate")
    @classmethod
    def _validate_sample_rate(cls, value: int) -> int:
        if value not in _VALID_TTS_SAMPLE_RATES:
            allowed = ", ".join(str(item) for item in sorted(_VALID_TTS_SAMPLE_RATES))
            raise ValueError(f"voice.tts.sample_rate must be one of: {allowed}")
        return value

    @model_validator(mode="after")
    def _validate_qwen_tts_options(self) -> "VoiceTTSConfig":
        self.audio_format = _normalize_provider_audio_format(self.provider, self.audio_format)
        if self.optimize_instructions and not self.instructions:
            raise ValueError("voice.tts.optimize_instructions requires voice.tts.instructions")
        return self


class VoiceAudioConfig(BaseModel):
    """Local audio device preferences for the voice channel."""

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
        normalized = value.strip()
        if not normalized:
            return "auto"
        return normalized


class VoiceChannelConfig(BaseModel):
    """User-facing configuration for `channels.voice`."""

    model_config = ConfigDict(extra="forbid")

    provider: str | None = None
    api_key: str | None = None
    websocket_base_url: str | None = None
    enable_interruptions: bool = False
    audio: VoiceAudioConfig = Field(default_factory=VoiceAudioConfig)
    stt: VoiceSTTConfig = Field(default_factory=VoiceSTTConfig)
    tts: VoiceTTSConfig = Field(default_factory=VoiceTTSConfig)

    @field_validator("provider")
    @classmethod
    def _validate_provider(cls, value: str | None) -> str | None:
        return _channel_voice_provider(value)

    @field_validator("websocket_base_url")
    @classmethod
    def _validate_websocket_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def _validate_nested_providers(self) -> "VoiceChannelConfig":
        if self.provider is None:
            return self
        if self.stt.provider != self.provider or self.tts.provider != self.provider:
            raise ValueError("channels.voice provider must match voice.stt.provider and voice.tts.provider")
        return self

    @classmethod
    def from_dict(cls, data: Any) -> "VoiceChannelConfig":
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ValueError("channels.voice must be a dictionary")
        data = _normalize_voice_config(data)
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            raise ValueError(str(exc)) from exc

    def resolved_api_key(self) -> str:
        provider = self.resolved_provider()
        api_key = str(self.api_key or "").strip()
        if not api_key or api_key in _VOICE_KEY_PLACEHOLDERS:
            raise ValueError(
                f"{provider} voice API key is required. Set channels.voice.api_key in config.yaml."
            )
        return api_key

    def resolved_provider(self) -> str:
        if self.provider is not None:
            return self.provider
        allowed = ", ".join(sorted(VOICE_PROVIDERS))
        raise ValueError(
            f"voice provider is required. Set channels.voice.provider to one of: {allowed}. "
            "Remove channels.voice if you are not using voice."
        )

    def resolved_websocket_base_url(self) -> str | None:
        if self.websocket_base_url is None:
            return None
        return self.websocket_base_url.strip() or None

    def tts_language_for(self, stt_language: str | None) -> str:
        if self.tts.language_policy == "from_stt_dominant":
            language = (stt_language or "").strip()
            if language:
                return language
        return self.tts.fallback_language


def _normalize_voice_config(data: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(data)
    declared_provider = _channel_voice_provider(normalized.get("provider"))

    stt = dict(normalized.get("stt") or {})
    stt_provider = _channel_voice_provider(stt.get("provider")) if "provider" in stt else None
    if "provider" in stt:
        if stt_provider is None:
            stt.pop("provider", None)
        else:
            stt["provider"] = stt_provider

    normalized["stt"] = stt

    tts = dict(normalized.get("tts") or {})
    tts_provider = _channel_voice_provider(tts.get("provider")) if "provider" in tts else None
    if "provider" in tts:
        if tts_provider is None:
            tts.pop("provider", None)
        else:
            tts["provider"] = tts_provider

    inferred_providers = {item for item in (stt_provider, tts_provider) if item is not None}
    if declared_provider is None:
        if len(inferred_providers) > 1:
            raise ValueError("channels.voice provider must match voice.stt.provider and voice.tts.provider")
        provider = next(iter(inferred_providers), None)
        if "provider" in normalized or provider is not None:
            normalized["provider"] = provider
    else:
        provider = declared_provider
        normalized["provider"] = provider

    if provider is not None:
        stt.setdefault("provider", provider)
        stt.setdefault("model", _DEFAULT_STT_MODELS[provider])
        stt.setdefault("audio_format", "pcm" if provider == VOICE_PROVIDER_QWEN else "pcm_s16le")
        if provider == VOICE_PROVIDER_QWEN:
            stt.setdefault("vad_threshold", _QWEN_VAD_THRESHOLD_DEFAULT)
            stt.setdefault("silence_duration_ms", _QWEN_SILENCE_DURATION_MS_DEFAULT)

        tts.setdefault("provider", provider)
        tts.setdefault("model", _DEFAULT_TTS_MODELS[provider])
        tts.setdefault("voice", _DEFAULT_TTS_VOICES[provider])
        tts.setdefault("audio_format", "pcm" if provider == VOICE_PROVIDER_QWEN else "pcm_s16le")

    normalized["tts"] = tts

    return normalized
