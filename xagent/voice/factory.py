"""Factory helpers for the default local voice runtime."""
from __future__ import annotations

from typing import Any

from .audio import AudioDevicePreference, SoundDeviceMicrophone, SoundDevicePlayer, resolve_audio_io_profile
from .config import VOICE_PROVIDER_QWEN, VOICE_PROVIDER_SONIOX, VoiceChannelConfig
from .runtime import VoiceRuntime, VoiceRuntimeOptions
from .qwen import create_qwen_adapters
from .soniox import create_soniox_adapters


def create_local_voice_runtime(
    *,
    agent: Any,
    config: VoiceChannelConfig,
    options: VoiceRuntimeOptions,
    input_device: AudioDevicePreference = None,
    output_device: AudioDevicePreference = None,
) -> VoiceRuntime:
    if config.provider == VOICE_PROVIDER_QWEN:
        recognizer, synthesizer = create_qwen_adapters(config)
    elif config.provider == VOICE_PROVIDER_SONIOX:
        recognizer, synthesizer = create_soniox_adapters(config)
    else:  # pragma: no cover - guarded by config validation
        raise ValueError(f"Unsupported voice provider: {config.provider}")
    audio_profile = resolve_audio_io_profile(
        input_sample_rate=config.stt.sample_rate,
        input_channels=config.stt.num_channels,
        output_sample_rate=config.tts.sample_rate,
        output_channels=1,
        input_device=input_device if input_device is not None else config.audio.input,
        output_device=output_device if output_device is not None else config.audio.output,
    )
    microphone = SoundDeviceMicrophone(
        sample_rate=config.stt.sample_rate,
        channels=config.stt.num_channels,
        device_index=audio_profile.input_selection.device_index,
        device_name=audio_profile.input_selection.device_name,
        stream_sample_rate=audio_profile.input_selection.stream_sample_rate,
        stream_channels=audio_profile.input_selection.stream_channels,
    )
    player = SoundDevicePlayer(
        sample_rate=config.tts.sample_rate,
        channels=1,
        device_index=audio_profile.output_selection.device_index,
        device_name=audio_profile.output_selection.device_name,
        stream_sample_rate=audio_profile.output_selection.stream_sample_rate,
        stream_channels=audio_profile.output_selection.stream_channels,
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
