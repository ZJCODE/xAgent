"""Factory helpers for the default local voice runtime."""
from __future__ import annotations

from typing import Any

from .audio import SoundDeviceMicrophone, SoundDevicePlayer
from .config import VOICE_PROVIDER_QWEN, VOICE_PROVIDER_SONIOX, VoiceChannelConfig
from .runtime import VoiceRuntime, VoiceRuntimeOptions
from .qwen import create_qwen_adapters
from .soniox import create_soniox_adapters


def create_local_voice_runtime(
    *,
    agent: Any,
    config: VoiceChannelConfig,
    options: VoiceRuntimeOptions,
) -> VoiceRuntime:
    if config.provider == VOICE_PROVIDER_QWEN:
        recognizer, synthesizer = create_qwen_adapters(config)
    elif config.provider == VOICE_PROVIDER_SONIOX:
        recognizer, synthesizer = create_soniox_adapters(config)
    else:  # pragma: no cover - guarded by config validation
        raise ValueError(f"Unsupported voice provider: {config.provider}")
    microphone = SoundDeviceMicrophone(
        sample_rate=config.stt.sample_rate,
        channels=config.stt.num_channels,
    )
    player = SoundDevicePlayer(
        sample_rate=config.tts.sample_rate,
        channels=1,
    )
    return VoiceRuntime(
        agent=agent,
        config=config,
        microphone=microphone,
        recognizer=recognizer,
        synthesizer=synthesizer,
        player=player,
        options=options,
    )
