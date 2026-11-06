"""Pure conversation floor state machine for voice turn-taking."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class FloorState(str, Enum):
    IDLE = "idle"
    USER_SPEAKING = "user_speaking"
    THINKING = "thinking"
    SPEAKING = "speaking"


class FloorEventKind(str, Enum):
    STT_ENDPOINT = "stt_endpoint"
    AGENT_FIRST_TEXT = "agent_first_text"
    PLAYBACK_STOPPED = "playback_stopped"
    BARGE_IN_CONFIRMED = "barge_in_confirmed"


@dataclass(frozen=True)
class FloorEvent:
    kind: FloorEventKind
    text: str = ""


class FloorCommandKind(str, Enum):
    MIC_OPEN = "mic_open"
    MIC_MUTE = "mic_mute"
    CANCEL_AGENT = "cancel_agent"
    CANCEL_TTS = "cancel_tts"
    EMIT_NOTICE = "emit_notice"


@dataclass(frozen=True)
class FloorCommand:
    kind: FloorCommandKind
    notice_category: str = ""


@dataclass
class ConversationFloor:
    """Track turn-taking and emit capture/playback controls, without I/O.

    VoiceRuntime owns transcript merging, model generation, and interruption
    metadata. The floor only handles events that runtime actually emits.
    """

    state: FloorState = FloorState.IDLE
    allow_duplex_capture: bool = False

    def handle(self, event: FloorEvent) -> list[FloorCommand]:
        handler = _HANDLERS.get((self.state, event.kind))
        if handler is None:
            return []
        return handler(self, event)


def _on_idle_stt_endpoint(floor: ConversationFloor, event: FloorEvent) -> list[FloorCommand]:
    if not event.text.strip():
        return [FloorCommand(FloorCommandKind.EMIT_NOTICE, notice_category="not_understood")]
    floor.state = FloorState.THINKING
    return [FloorCommand(FloorCommandKind.MIC_OPEN if floor.allow_duplex_capture else FloorCommandKind.MIC_MUTE)]


def _on_thinking_stt_endpoint(_floor: ConversationFloor, _event: FloorEvent) -> list[FloorCommand]:
    return [FloorCommand(FloorCommandKind.CANCEL_AGENT)]


def _on_thinking_agent_first_text(floor: ConversationFloor, _event: FloorEvent) -> list[FloorCommand]:
    floor.state = FloorState.SPEAKING
    return []


def _on_speaking_barge_in(floor: ConversationFloor, _event: FloorEvent) -> list[FloorCommand]:
    floor.state = FloorState.USER_SPEAKING
    return [
        FloorCommand(FloorCommandKind.CANCEL_TTS),
        FloorCommand(FloorCommandKind.CANCEL_AGENT),
        FloorCommand(FloorCommandKind.MIC_OPEN),
    ]


def _on_speaking_playback_stopped(floor: ConversationFloor, _event: FloorEvent) -> list[FloorCommand]:
    floor.state = FloorState.IDLE
    return [FloorCommand(FloorCommandKind.MIC_OPEN)]


_HANDLERS = {
    (FloorState.IDLE, FloorEventKind.STT_ENDPOINT): _on_idle_stt_endpoint,
    (FloorState.THINKING, FloorEventKind.STT_ENDPOINT): _on_thinking_stt_endpoint,
    (FloorState.THINKING, FloorEventKind.AGENT_FIRST_TEXT): _on_thinking_agent_first_text,
    (FloorState.SPEAKING, FloorEventKind.BARGE_IN_CONFIRMED): _on_speaking_barge_in,
    (FloorState.SPEAKING, FloorEventKind.PLAYBACK_STOPPED): _on_speaking_playback_stopped,
}
