"""Factory helpers for the Soniox-only local voice runtime."""
from __future__ import annotations

import logging
from typing import Any

from .audio import (
    AudioDevicePreference,
    AudioIOProfile,
    SoundDeviceMicrophone,
    SoundDevicePlayer,
    resolve_audio_io_profile,
)
from .attention import VoiceAttentionConfig, VoiceAttentionGate, normalize_terms
from .context_terms import agent_identity_terms, startup_context_terms
from .config import (
    SONIOX_STT_CHANNELS,
    SONIOX_STT_SAMPLE_RATE,
    SONIOX_TTS_CHANNELS,
    SONIOX_TTS_SAMPLE_RATE,
    VoiceChannelConfig,
    VoiceProfileName,
    VoiceRuntimeProfile,
    VoiceSpeechStyle,
)
from .presence import SttLifecycleController, VoicePresenceConfig
from .runtime import VoiceRuntime, VoiceRuntimeOptions
from .soniox import SonioxSTTCallbacks, create_soniox_adapters

logger = logging.getLogger(__name__)


def resolve_runtime_profile(
    audio_profile: AudioIOProfile,
    *,
    profile_override: VoiceProfileName | None = None,
    interruptions_override: bool | None = None,
) -> VoiceRuntimeProfile:
    """Turn detected device topology, plus any session override, into policy."""
    topology = audio_profile.topology
    detected_name: VoiceProfileName = "headset" if topology.near_field else "room"
    name = profile_override or detected_name
    echo_managed = (
        topology.echo_managed if interruptions_override is None else interruptions_override
    )
    sources = [] if profile_override is None else [f"profile={profile_override} (override)"]
    if interruptions_override is not None:
        sources.append(f"interruptions={'on' if interruptions_override else 'off'} (override)")
    source = ", ".join(sources) if sources else topology.reason
    return VoiceRuntimeProfile(name=name, echo_managed=echo_managed, source=source)


def create_local_voice_runtime(
    *,
    agent: Any,
    config: VoiceChannelConfig,
    options: VoiceRuntimeOptions,
    input_device: AudioDevicePreference = None,
    output_device: AudioDevicePreference = None,
    profile_override: VoiceProfileName | None = None,
    interruptions_override: bool | None = None,
    speed_override: float | None = None,
) -> VoiceRuntime:
    runtime_holder: list[VoiceRuntime | None] = [None]

    def _on_stt_reconnecting() -> None:
        runtime = runtime_holder[0]
        if runtime is not None:
            runtime._schedule_notice("ears_offline")

    def _on_stt_recovered() -> None:
        runtime = runtime_holder[0]
        if runtime is not None:
            runtime._schedule_notice("back_online")

    audio_profile = resolve_audio_io_profile(
        input_sample_rate=SONIOX_STT_SAMPLE_RATE,
        input_channels=SONIOX_STT_CHANNELS,
        output_sample_rate=SONIOX_TTS_SAMPLE_RATE,
        output_channels=SONIOX_TTS_CHANNELS,
        input_device=input_device if input_device is not None else config.audio.input,
        output_device=output_device if output_device is not None else config.audio.output,
    )
    # Diarization, attention and endpointing all read the profile, and the STT
    # session is configured from it, so settle it before the adapters exist.
    runtime_profile = resolve_runtime_profile(
        audio_profile,
        profile_override=profile_override,
        interruptions_override=interruptions_override,
    )
    config.apply_runtime_profile(runtime_profile)
    default_style = VoiceSpeechStyle()
    config.apply_speech_style(
        VoiceSpeechStyle(
            voice=str(getattr(agent, "voice", "") or "").strip() or default_style.voice,
            speed=default_style.speed if speed_override is None else speed_override,
        )
    )
    logger.info(
        "Voice profile: %s, barge-in %s (%s)",
        runtime_profile.name,
        "on" if runtime_profile.echo_managed else "off",
        runtime_profile.source,
    )

    presence_cfg = VoicePresenceConfig(
        close_stt_after_idle_seconds=config.presence.close_stt_after_idle_seconds,
        wake_energy_rms=config.presence.wake_energy_rms,
        recent_speech_hours=config.presence.recent_speech_hours,
    )
    lifecycle = SttLifecycleController(presence_cfg)
    recognizer, synthesizer = create_soniox_adapters(
        config,
        stt_callbacks=SonioxSTTCallbacks(
            on_reconnecting=_on_stt_reconnecting,
            on_recovered=_on_stt_recovered,
        ),
        lifecycle=lifecycle,
    )
    extra_terms = startup_context_terms(agent)
    set_terms = getattr(recognizer, "set_extra_context_terms", None)
    if callable(set_terms):
        set_terms(extra_terms)
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
        keep_warm=config.performance.warm_output_device,
    )
    # Only the agent's own name wakes it; the other context terms are there so
    # the recognizer spells people correctly, not so any mention grabs the floor.
    wake_terms = normalize_terms(list(config.attention.wake_terms) + agent_identity_terms(agent))
    attention_gate = VoiceAttentionGate(
        config=VoiceAttentionConfig(
            open_window_seconds=config.attention.open_window_seconds,
            wake_terms=list(config.attention.wake_terms),
            use_decide_participation=config.attention.use_decide_participation,
        ),
        wake_terms=wake_terms,
    )
    runtime = VoiceRuntime(
        agent=agent,
        config=config,
        microphone=microphone,
        recognizer=recognizer,
        synthesizer=synthesizer,
        player=player,
        options=options,
        attention_gate=attention_gate,
        stt_lifecycle=lifecycle,
    )
    runtime_holder[0] = runtime
    return runtime
