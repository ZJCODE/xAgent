"""Configuration for the Soniox-only local voice channel."""
from __future__ import annotations

import difflib
import os
import re
from typing import Any, Literal

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

VoiceProfileName = Literal["room", "headset"]

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

_VOICE_PUBLIC_KEYS = frozenset(
    {
        "api_key",
        "profile",
        "voice",
        "speed",
        "language_hints",
        "fallback_language",
        "names",
        "interruptions",
        "quiet_hours",
        "idle_shutdown_minutes",
        "audio",
    }
)

_VOICE_KNOWN_TOP_LEVEL_KEYS = frozenset(
    {
        *_VOICE_PUBLIC_KEYS,
        "enable_interruptions",
        "context",
        "attention",
        "presence",
        "proactive",
        "performance",
        "interruption",
        "enable_diarization",
        "room_name",
        "return_timestamps",
        "aggregate_utterances",
    }
)

VOICE_CONFIG_EXAMPLE = """channels:
  voice:
    api_key: your_soniox_api_key_here
    profile: room
    voice: Owen
    speed: 1.0
    language_hints: [zh, en]
    fallback_language: zh
    names: []
    interruptions: false
    quiet_hours: "22:00-07:00"
    idle_shutdown_minutes: 0
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


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _profile_preset(profile: str) -> dict[str, Any]:
    normalized = (profile or "room").strip().lower()
    if normalized == "headset":
        return {
            "enable_diarization": False,
            "attention": {
                "open_window_seconds": 120.0,
                "use_decide_participation": False,
            },
            "interruption": {"min_words": 1, "min_speech_ms": 150},
        }
    return {
        "enable_diarization": True,
        "attention": {
            "open_window_seconds": 25.0,
            "use_decide_participation": True,
        },
        "interruption": {"min_words": 2, "min_speech_ms": 250},
    }


def parse_quiet_hours(value: str | None) -> tuple[int, int]:
    """Parse ``HH:MM-HH:MM`` or return (22, 7) for empty/disabled."""
    raw = (value or "").strip()
    if not raw:
        return 22, 7
    match = re.match(
        r"^(\d{1,2})(?::(\d{2}))?\s*-\s*(\d{1,2})(?::(\d{2}))?$",
        raw,
    )
    if not match:
        raise ValueError(
            'voice.quiet_hours must look like "22:00-07:00" or "" to use defaults'
        )
    start_h = int(match.group(1))
    end_h = int(match.group(3))
    if not (0 <= start_h <= 23 and 0 <= end_h <= 23):
        raise ValueError("voice.quiet_hours hours must be between 0 and 23")
    return start_h, end_h


def format_quiet_hours(start: int, end: int) -> str:
    if start == end:
        return ""
    return f"{start:02d}:00-{end:02d}:00"


def _normalize_voice_dict(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(data)
    profile = str(out.get("profile") or "room").strip().lower()
    if profile not in {"room", "headset"}:
        raise ValueError('voice.profile must be "room" or "headset"')
    out["profile"] = profile

    preset = _profile_preset(profile)
    out = _deep_merge(preset, out)

    if "names" in out:
        names = out.pop("names")
        if names is None:
            names = []
        if not isinstance(names, list):
            raise ValueError("voice.names must be a list of strings")
        ctx = out.setdefault("context", {})
        if not isinstance(ctx, dict):
            raise ValueError("voice.context must be a dictionary")
        existing = ctx.get("terms") if isinstance(ctx.get("terms"), list) else []
        merged_terms = list(
            dict.fromkeys(
                [term.strip() for term in existing if str(term).strip()]
                + [str(term).strip() for term in names if str(term).strip()]
            )
        )
        ctx["terms"] = merged_terms

    if "interruptions" in out:
        out.pop("enable_interruptions", None)
        out["enable_interruptions"] = bool(out.pop("interruptions"))
    elif "enable_interruptions" in out:
        out["enable_interruptions"] = bool(out["enable_interruptions"])

    if "quiet_hours" in out:
        quiet = out.pop("quiet_hours")
        if quiet is None:
            quiet = ""
        start, end = parse_quiet_hours(str(quiet))
        proactive = out.setdefault("proactive", {})
        if not isinstance(proactive, dict):
            raise ValueError("voice.proactive must be a dictionary")
        proactive["quiet_hours_start"] = start
        proactive["quiet_hours_end"] = end

    if "idle_shutdown_minutes" in out:
        raw_minutes = out.pop("idle_shutdown_minutes")
        try:
            minutes = float(raw_minutes)
        except (TypeError, ValueError) as exc:
            raise ValueError("voice.idle_shutdown_minutes must be a number") from exc
        if minutes < 0:
            raise ValueError("voice.idle_shutdown_minutes must be >= 0")
        presence = out.setdefault("presence", {})
        if not isinstance(presence, dict):
            raise ValueError("voice.presence must be a dictionary")
        presence["close_stt_after_idle_seconds"] = minutes * 60.0

    audio = out.get("audio")
    if isinstance(audio, dict):
        audio = dict(audio)
        audio.pop("echo_cancellation", None)
        out["audio"] = audio

    out.pop("return_timestamps", None)
    out["return_timestamps"] = True
    out["aggregate_utterances"] = True
    return out


def _suggest_voice_key(bad_key: str) -> str:
    matches = difflib.get_close_matches(
        bad_key,
        sorted(_VOICE_KNOWN_TOP_LEVEL_KEYS),
        n=1,
        cutoff=0.6,
    )
    if matches:
        return f" Did you mean channels.voice.{matches[0]}?"
    return ""


def _format_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors():
        if error.get("type") == "extra_forbidden":
            loc = error.get("loc") or ()
            key = loc[-1] if loc else "?"
            path = ".".join(str(item) for item in loc)
            hint = _suggest_voice_key(str(key))
            parts.append(f"Unknown voice setting {path!r}.{hint}")
            continue
        loc = ".".join(str(item) for item in (error.get("loc") or ()))
        msg = error.get("msg") or "invalid value"
        prefix = f"channels.voice.{loc}" if loc else "channels.voice"
        parts.append(f"{prefix}: {msg}")
    return "; ".join(parts) if parts else str(exc)


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


class VoicePerformanceConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preemptive_generation: bool = True
    preemptive_min_chars: int = Field(default=8, ge=3, le=500)
    instant_ack: bool = True
    ack_delay_ms: float = Field(default=400.0, ge=0.0, le=5_000.0)
    ack_cooldown_seconds: float = Field(default=45.0, ge=0.0, le=600.0)
    warm_output_device: bool = True
    max_agent_loops: int = Field(default=12, ge=1, le=50)
    speak_tool_progress: bool = True


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
    """Configuration for ``channels.voice``."""

    model_config = ConfigDict(extra="forbid")

    api_key: str | None = None
    profile: VoiceProfileName = "room"
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
    performance: VoicePerformanceConfigModel = Field(default_factory=VoicePerformanceConfigModel)
    context: SonioxSTTContextConfig = Field(default_factory=SonioxSTTContextConfig)
    audio: VoiceAudioConfig = Field(default_factory=VoiceAudioConfig)

    @model_validator(mode="before")
    @classmethod
    def _prepare_configuration(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        legacy_keys = _LEGACY_VOICE_KEYS.intersection(value)
        if legacy_keys:
            raise _migration_error(legacy_keys)
        return _normalize_voice_dict(value)

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
            raise ValueError(_format_validation_error(exc)) from exc

    @classmethod
    def default_public_dict(cls, *, api_key: str | None = None) -> dict[str, Any]:
        """Tier-1 keys written into config.yaml for a new voice channel."""
        config = cls.from_dict({"api_key": api_key or SONIOX_KEY_PLACEHOLDER})
        return config.to_public_dict()

    def to_public_dict(self) -> dict[str, Any]:
        """Serialize the supported user-facing surface (Tier 1)."""
        idle_minutes = int(round(self.presence.close_stt_after_idle_seconds / 60.0))
        payload: dict[str, Any] = {
            "api_key": self.api_key or SONIOX_KEY_PLACEHOLDER,
            "profile": self.profile,
            "voice": self.voice,
            "speed": self.speed,
            "language_hints": list(self.language_hints),
            "fallback_language": self.fallback_language,
            "names": list(self.context.terms),
            "interruptions": self.enable_interruptions,
            "quiet_hours": format_quiet_hours(
                self.proactive.quiet_hours_start,
                self.proactive.quiet_hours_end,
            ),
            "idle_shutdown_minutes": idle_minutes,
            "audio": {
                "input": self.audio.input,
                "output": self.audio.output,
            },
        }
        if not payload["quiet_hours"]:
            payload["quiet_hours"] = format_quiet_hours(22, 7)
        return payload

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

    @property
    def stt_endpoint_sensitivity(self) -> float:
        if self.profile == "headset":
            return 0.4
        return SONIOX_ENDPOINT_SENSITIVITY

    @property
    def stt_max_endpoint_delay_ms(self) -> int:
        if self.profile == "headset":
            return 1_000
        return SONIOX_MAX_ENDPOINT_DELAY_MS

    @property
    def stt_endpoint_latency_level(self) -> int:
        return SONIOX_ENDPOINT_LATENCY_LEVEL

    @property
    def aggregation_grace_scale(self) -> float:
        return 0.85 if self.profile == "headset" else 1.0

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
