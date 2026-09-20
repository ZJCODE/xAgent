"""Configuration for the Soniox-only local voice channel."""
from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)

from .presence import VOICE_STT_IDLE_SHUTDOWN_SECONDS

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


@dataclass(frozen=True)
class VoiceRuntimeProfile:
    """Behaviour derived from the audio devices actually in use.

    Near-field and far-field capture want opposite settings for diarization,
    attention and endpointing, and no user can set those coherently. The
    devices answer the question, and they answer it per session: the same
    machine moves between earbuds and a room speaker within a day.
    """

    name: VoiceProfileName = "room"
    echo_managed: bool = False
    source: str = "default"


@dataclass(frozen=True)
class VoiceSpeechStyle:
    """How the agent sounds. The voice belongs to the agent's identity, and
    rate is a listener's preference, so neither is a property of the channel."""

    voice: str = "Owen"
    speed: float = 1.0

_VOICE_KEY_PLACEHOLDERS = {
    SONIOX_KEY_PLACEHOLDER,
    "your_qwen_api_key_here",
    "your_api_key_here",
}

_VOICE_TOP_LEVEL_KEYS = frozenset(
    {
        "api_key",
        "language_hints",
        "fallback_language",
        "quiet_hours",
        "audio",
    }
)

VOICE_CONFIG_EXAMPLE = """channels:
  voice:
    api_key: your_soniox_api_key_here
    language_hints: [zh, en]
    fallback_language: zh
    quiet_hours: "22:00-07:00"
    audio:
      input: auto
      output: auto"""


def parse_quiet_hours(value: str | None) -> tuple[int, int]:
    """Parse ``HH:MM-HH:MM``. Empty string means no quiet window."""
    raw = (value or "").strip()
    if not raw:
        return 0, 0
    match = re.match(
        r"^(\d{1,2})(?::(\d{2}))?\s*-\s*(\d{1,2})(?::(\d{2}))?$",
        raw,
    )
    if not match:
        raise ValueError('voice.quiet_hours must look like "22:00-07:00" or ""')
    start_h = int(match.group(1))
    end_h = int(match.group(3))
    if not (0 <= start_h <= 23 and 0 <= end_h <= 23):
        raise ValueError("voice.quiet_hours hours must be between 0 and 23")
    return start_h, end_h


def format_quiet_hours(start: int, end: int) -> str:
    if start == end:
        return ""
    return f"{start:02d}:00-{end:02d}:00"


def _suggest_voice_key(bad_key: str) -> str:
    matches = difflib.get_close_matches(
        bad_key,
        sorted(_VOICE_TOP_LEVEL_KEYS),
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


class VoiceAttentionConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    open_window_seconds: float = Field(default=25.0, ge=0.0, le=120.0)
    wake_terms: list[str] = Field(default_factory=list)
    use_decide_participation: bool = True


class VoicePresenceConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    close_stt_after_idle_seconds: float = Field(
        default=VOICE_STT_IDLE_SHUTDOWN_SECONDS, ge=0.0, le=86_400.0
    )
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
    model_config = ConfigDict(extra="forbid")

    min_speech_ms: int = Field(default=250, ge=50, le=2_000)
    min_words: int = Field(default=2, ge=1, le=8)


class VoiceAudioConfig(BaseModel):
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


_DEFAULT_PERFORMANCE = VoicePerformanceConfigModel()
_DEFAULT_PROACTIVE_TAIL = VoiceProactiveConfigModel()
_PRESENCE_WAKE_RMS = 450.0
_PRESENCE_RECENT_SPEECH_HOURS = 6.0
_ROOM_NAME = "local_room"


class VoiceChannelConfig(BaseModel):
    """User-facing ``channels.voice`` configuration."""

    model_config = ConfigDict(extra="forbid")

    api_key: str | None = None
    language_hints: list[str] = Field(default_factory=lambda: ["zh", "en"])
    fallback_language: str = "zh"
    quiet_hours: str = "22:00-07:00"
    audio: VoiceAudioConfig = Field(default_factory=VoiceAudioConfig)

    _runtime_profile: VoiceRuntimeProfile = PrivateAttr(default_factory=VoiceRuntimeProfile)
    _speech_style: VoiceSpeechStyle = PrivateAttr(default_factory=VoiceSpeechStyle)

    @field_validator("api_key")
    @classmethod
    def _validate_api_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("fallback_language")
    @classmethod
    def _validate_non_empty(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("fallback_language must be non-empty")
        return normalized

    @field_validator("language_hints")
    @classmethod
    def _validate_language_hints(cls, value: list[str]) -> list[str]:
        hints = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        if not hints:
            raise ValueError("voice.language_hints must include at least one language")
        return hints

    @field_validator("quiet_hours")
    @classmethod
    def _validate_quiet_hours(cls, value: str) -> str:
        parse_quiet_hours(value)
        return value.strip()

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
        config = cls.from_dict({"api_key": api_key or SONIOX_KEY_PLACEHOLDER})
        return config.to_public_dict()

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "api_key": self.api_key or SONIOX_KEY_PLACEHOLDER,
            "language_hints": list(self.language_hints),
            "fallback_language": self.fallback_language,
            "quiet_hours": self.quiet_hours,
            "audio": {
                "input": self.audio.input,
                "output": self.audio.output,
            },
        }

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

    def apply_runtime_profile(self, profile: VoiceRuntimeProfile) -> None:
        self._runtime_profile = profile

    def apply_speech_style(self, style: VoiceSpeechStyle) -> None:
        self._speech_style = style

    @property
    def voice(self) -> str:
        return self._speech_style.voice

    @property
    def speed(self) -> float:
        return self._speech_style.speed

    @property
    def runtime_profile(self) -> VoiceRuntimeProfile:
        return self._runtime_profile

    @property
    def profile(self) -> VoiceProfileName:
        return self._runtime_profile.name

    @property
    def room_name(self) -> str:
        return _ROOM_NAME

    @property
    def enable_diarization(self) -> bool:
        return self.profile == "room"

    @property
    def return_timestamps(self) -> bool:
        return True

    @property
    def aggregate_utterances(self) -> bool:
        return True

    @property
    def enable_interruptions(self) -> bool:
        return self._runtime_profile.echo_managed

    @property
    def attention(self) -> VoiceAttentionConfigModel:
        if self.profile == "headset":
            return VoiceAttentionConfigModel(
                open_window_seconds=120.0,
                use_decide_participation=False,
            )
        return VoiceAttentionConfigModel(
            open_window_seconds=25.0,
            use_decide_participation=True,
        )

    @property
    def interruption(self) -> VoiceInterruptionConfig:
        if self.profile == "headset":
            return VoiceInterruptionConfig(min_words=1, min_speech_ms=150)
        return VoiceInterruptionConfig(min_words=2, min_speech_ms=250)

    @property
    def proactive(self) -> VoiceProactiveConfigModel:
        start, end = parse_quiet_hours(self.quiet_hours)
        return VoiceProactiveConfigModel(
            quiet_hours_start=start,
            quiet_hours_end=end,
            max_per_hour=_DEFAULT_PROACTIVE_TAIL.max_per_hour,
            require_recent_speech=_DEFAULT_PROACTIVE_TAIL.require_recent_speech,
        )

    @property
    def presence(self) -> VoicePresenceConfigModel:
        return VoicePresenceConfigModel(
            close_stt_after_idle_seconds=VOICE_STT_IDLE_SHUTDOWN_SECONDS,
            wake_energy_rms=_PRESENCE_WAKE_RMS,
            recent_speech_hours=_PRESENCE_RECENT_SPEECH_HOURS,
        )

    @property
    def performance(self) -> VoicePerformanceConfigModel:
        return _DEFAULT_PERFORMANCE

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
        return (stt_language or "").strip() or self.fallback_language

    def merged_stt_context_terms(self, extra_terms: list[str] | None = None) -> list[str]:
        terms: list[str] = []
        for term in extra_terms or []:
            cleaned = term.strip()
            if cleaned and cleaned not in terms:
                terms.append(cleaned)
        for term in self.attention.wake_terms:
            if term not in terms:
                terms.append(term)
        return terms

    def merged_stt_context_payload(self, extra_terms: list[str] | None = None) -> dict[str, Any] | None:
        terms = self.merged_stt_context_terms(extra_terms)
        if not terms:
            return None
        return {"terms": terms}
