"""Local voice runtime for xAgent."""
from __future__ import annotations

__all__ = [
    "VoiceChannelConfig",
    "VoiceAudioConfig",
    "VoiceRuntime",
    "VoiceRuntimeOptions",
    "VoiceSTTConfig",
    "VoiceTTSConfig",
    "VoiceUtterance",
]


def __getattr__(name: str):
    if name in {
        "VoiceAudioConfig",
        "VoiceChannelConfig",
        "VoiceSTTConfig",
        "VoiceTTSConfig",
    }:
        from .config import (
            VoiceAudioConfig,
            VoiceChannelConfig,
            VoiceSTTConfig,
            VoiceTTSConfig,
        )

        return {
            "VoiceAudioConfig": VoiceAudioConfig,
            "VoiceChannelConfig": VoiceChannelConfig,
            "VoiceSTTConfig": VoiceSTTConfig,
            "VoiceTTSConfig": VoiceTTSConfig,
        }[name]
    if name in {"VoiceRuntime", "VoiceRuntimeOptions", "VoiceUtterance"}:
        from .runtime import VoiceRuntime, VoiceRuntimeOptions, VoiceUtterance

        return {
            "VoiceRuntime": VoiceRuntime,
            "VoiceRuntimeOptions": VoiceRuntimeOptions,
            "VoiceUtterance": VoiceUtterance,
        }[name]
    raise AttributeError(name)
