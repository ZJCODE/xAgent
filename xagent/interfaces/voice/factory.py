"""Factory helpers for the Soniox voice runtime over WebSocket."""
from __future__ import annotations

from typing import Any

from .attention import VoiceAttentionConfig, VoiceAttentionGate, normalize_terms
from .context_terms import agent_identity_terms, startup_context_terms
from .config import (
    SONIOX_STT_CHANNELS,
    SONIOX_STT_SAMPLE_RATE,
    SONIOX_TTS_CHANNELS,
    SONIOX_TTS_SAMPLE_RATE,
    VoiceChannelConfig,
    VoiceRuntimeProfile,
    VoiceSpeechStyle,
)
from .presence import SttLifecycleController, VoicePresenceConfig
from .runtime import VoiceRuntime, VoiceRuntimeOptions
from .soniox import SonioxSTTCallbacks, create_soniox_adapters
from .ws import (
    WebSocketMicrophone,
    WebSocketPlayer,
    WebSocketVoiceBridge,
    make_websocket_output_sink,
)


def create_websocket_voice_runtime(
    *,
    agent: Any,
    config: VoiceChannelConfig,
    bridge: WebSocketVoiceBridge,
    options: VoiceRuntimeOptions,
    device_id: str = "",
) -> tuple[VoiceRuntime, WebSocketMicrophone, WebSocketPlayer]:
    """Construct a VoiceRuntime backed by a paired device WebSocket audio stream."""
    runtime_profile = VoiceRuntimeProfile(
        name=bridge.hello.profile,
        echo_managed=bridge.hello.echo_managed,
        source=f"websocket:{device_id or 'device'}",
        interruptions_override=bridge.hello.interruptions,
        room_name=bridge.room_name,
    )
    config.apply_runtime_profile(runtime_profile)
    config.apply_speech_style(
        VoiceSpeechStyle(
            voice=config.speaking_voice,
            speed=config.speaking_speed,
        )
    )
    presence_cfg = VoicePresenceConfig(
        # An explicitly active browser conversation stays ready until ended.
        close_stt_after_idle_seconds=0 if options.direct_conversation else config.presence.close_stt_after_idle_seconds,
        wake_energy_rms=config.presence.wake_energy_rms,
        recent_speech_hours=config.presence.recent_speech_hours,
    )
    lifecycle = SttLifecycleController(presence_cfg)
    recognizer, synthesizer = create_soniox_adapters(
        config,
        # Browser capture must stop while STT is unavailable. Closing this
        # session uses the browser's existing bounded reconnect and cleanup.
        stt_callbacks=SonioxSTTCallbacks(
            on_reconnecting=bridge.recognition_unavailable if options.direct_conversation else None,
            on_disconnected=bridge.recognition_unavailable if options.direct_conversation else None,
        ),
        lifecycle=lifecycle,
    )
    extra_terms = startup_context_terms(agent)
    set_terms = getattr(recognizer, "set_extra_context_terms", None)
    if callable(set_terms):
        set_terms(extra_terms)
    microphone = WebSocketMicrophone(
        bridge,
        sample_rate=SONIOX_STT_SAMPLE_RATE,
        channels=SONIOX_STT_CHANNELS,
    )
    player = WebSocketPlayer(
        bridge,
        sample_rate=SONIOX_TTS_SAMPLE_RATE,
        channels=SONIOX_TTS_CHANNELS,
        playback_flow_control=options.direct_conversation,
    )
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
        output=make_websocket_output_sink(bridge),
        attention_gate=attention_gate,
        stt_lifecycle=lifecycle,
    )
    return runtime, microphone, player
