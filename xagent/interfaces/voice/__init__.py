"""WebSocket Soniox voice runtime for xAgent."""
from __future__ import annotations

from .config import VoiceChannelConfig
from .runtime import VoiceRuntime, VoiceRuntimeOptions
from .types import VoiceUtterance
from .ws import (
    parse_client_control,
    VoiceSessionHello,
    WebSocketMicrophone,
    WebSocketPlayer,
    WebSocketVoiceBridge,
)

__all__ = [
    "VoiceChannelConfig",
    "VoiceRuntime",
    "VoiceRuntimeOptions",
    "VoiceUtterance",
    "parse_client_control",
    "VoiceSessionHello",
    "WebSocketMicrophone",
    "WebSocketPlayer",
    "WebSocketVoiceBridge",
]
