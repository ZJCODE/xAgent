"""Factory helpers for the default local voice runtime."""
from __future__ import annotations

from typing import Any

from .audio import AudioDevicePreference, SoundDeviceMicrophone, SoundDevicePlayer, resolve_audio_io_profile
from .config import VOICE_PROVIDER_QWEN, VOICE_PROVIDER_SONIOX, VoiceChannelConfig
from .runtime import VoiceRuntime, VoiceRuntimeOptions
from .qwen import (
    QWEN_REALTIME_WEBSOCKET_BASE_URL,
    QwenRealtimeSTT,
    QwenRealtimeTTS,
    create_qwen_adapters,
)
from .soniox import SonioxRealtimeSTT, SonioxRealtimeTTS, create_soniox_adapters


def create_local_voice_runtime(
    *,
    agent: Any,
    config: VoiceChannelConfig,
    options: VoiceRuntimeOptions,
    input_device: AudioDevicePreference = None,
    output_device: AudioDevicePreference = None,
) -> VoiceRuntime:
    provider = config.resolved_provider()
    if config.stt.provider == config.tts.provider == provider:
        if provider == VOICE_PROVIDER_QWEN:
            recognizer, synthesizer = create_qwen_adapters(config)
        elif provider == VOICE_PROVIDER_SONIOX:
            recognizer, synthesizer = create_soniox_adapters(config)
        else:  # pragma: no cover - guarded by config validation
            raise ValueError(f"Unsupported voice provider: {provider}")
    else:
        recognizer = _create_stt_adapter(config)
        synthesizer = _create_tts_adapter(config)
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


def _create_stt_adapter(config: VoiceChannelConfig) -> Any:
    if config.stt.provider == VOICE_PROVIDER_QWEN:
        websocket_base_url = config.resolved_websocket_base_url() or QWEN_REALTIME_WEBSOCKET_BASE_URL
        return QwenRealtimeSTT(
            api_key=config.resolved_stt_api_key(),
            config=config.stt,
            websocket_base_url=websocket_base_url,
        )
    if config.stt.provider == VOICE_PROVIDER_SONIOX:
        return SonioxRealtimeSTT(api_key=config.resolved_stt_api_key(), config=config.stt)
    raise ValueError(f"Unsupported STT provider: {config.stt.provider}")


def _create_tts_adapter(config: VoiceChannelConfig) -> Any:
    if config.tts.provider == VOICE_PROVIDER_QWEN:
        websocket_base_url = config.resolved_websocket_base_url() or QWEN_REALTIME_WEBSOCKET_BASE_URL
        return QwenRealtimeTTS(
            api_key=config.resolved_tts_api_key(),
            config=config.tts,
            websocket_base_url=websocket_base_url,
        )
    if config.tts.provider == VOICE_PROVIDER_SONIOX:
        return SonioxRealtimeTTS(api_key=config.resolved_tts_api_key(), config=config.tts)
    raise ValueError(f"Unsupported TTS provider: {config.tts.provider}")
