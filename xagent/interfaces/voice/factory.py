"""Factory helpers for the Soniox-only local voice runtime."""
from __future__ import annotations

from typing import Any

from .audio import (
    AudioDevicePreference,
    SoundDeviceMicrophone,
    SoundDevicePlayer,
    resolve_audio_io_profile,
)
from .config import (
    SONIOX_STT_CHANNELS,
    SONIOX_STT_SAMPLE_RATE,
    SONIOX_TTS_CHANNELS,
    SONIOX_TTS_SAMPLE_RATE,
    VoiceChannelConfig,
)
from .runtime import VoiceRuntime, VoiceRuntimeOptions
from .soniox import create_soniox_adapters


def create_local_voice_runtime(
    *,
    agent: Any,
    config: VoiceChannelConfig,
    options: VoiceRuntimeOptions,
    input_device: AudioDevicePreference = None,
    output_device: AudioDevicePreference = None,
) -> VoiceRuntime:
    recognizer, synthesizer = create_soniox_adapters(config)
    audio_profile = resolve_audio_io_profile(
        input_sample_rate=SONIOX_STT_SAMPLE_RATE,
        input_channels=SONIOX_STT_CHANNELS,
        output_sample_rate=SONIOX_TTS_SAMPLE_RATE,
        output_channels=SONIOX_TTS_CHANNELS,
        input_device=input_device if input_device is not None else config.audio.input,
        output_device=output_device if output_device is not None else config.audio.output,
    )
    microphone = SoundDeviceMicrophone(
        sample_rate=SONIOX_STT_SAMPLE_RATE,
        channels=SONIOX_STT_CHANNELS,
        device_index=audio_profile.input_selection.device_index,
        device_name=audio_profile.input_selection.device_name,
        stream_sample_rate=audio_profile.input_selection.stream_sample_rate,
        stream_channels=audio_profile.input_selection.stream_channels,
    )
    player = SoundDevicePlayer(
        sample_rate=SONIOX_TTS_SAMPLE_RATE,
        channels=SONIOX_TTS_CHANNELS,
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
