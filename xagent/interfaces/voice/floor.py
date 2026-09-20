"""Pure conversation floor state machine for voice turn-taking."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FloorState(str, Enum):
    IDLE = "idle"
    USER_SPEAKING = "user_speaking"
    AGGREGATING = "aggregating"
    THINKING = "thinking"
    SPEAKING = "speaking"
    EARS_DOWN = "ears_down"


class FloorEventKind(str, Enum):
    VAD_START = "vad_start"
    VAD_END = "vad_end"
    STT_PARTIAL = "stt_partial"
    STT_ENDPOINT = "stt_endpoint"
    AGGREGATION_READY = "aggregation_ready"
    AGENT_FIRST_TEXT = "agent_first_text"
    AGENT_DONE = "agent_done"
    AGENT_ERROR = "agent_error"
    PLAYBACK_STARTED = "playback_started"
    PLAYBACK_STOPPED = "playback_stopped"
    BARGE_IN_CONFIRMED = "barge_in_confirmed"
    STT_SESSION_LOST = "stt_session_lost"
    STT_SESSION_RESTORED = "stt_session_restored"
    STOP = "stop"


@dataclass(frozen=True)
class FloorEvent:
    kind: FloorEventKind
    text: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


class FloorCommandKind(str, Enum):
    MIC_OPEN = "mic_open"
    MIC_MUTE = "mic_mute"
    START_THINK = "start_think"
    STEER_THINK = "steer_think"
    CANCEL_AGENT = "cancel_agent"
    CANCEL_TTS = "cancel_tts"
    EMIT_NOTICE = "emit_notice"
    MARK_INTERRUPTED = "mark_interrupted"


@dataclass(frozen=True)
class FloorCommand:
    kind: FloorCommandKind
    text: str = ""
    notice_category: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConversationFloor:
    """Event-driven floor holder with no I/O."""

    state: FloorState = FloorState.IDLE
    allow_duplex_capture: bool = False
    pending_transcript: str = ""
    in_flight_transcript: str = ""

    def handle(self, event: FloorEvent) -> list[FloorCommand]:
        handler = _HANDLERS.get((self.state, event.kind))
        if handler is None:
            return []
        return handler(self, event)


def _commands(*items: FloorCommand) -> list[FloorCommand]:
    return list(items)


def _on_idle_stt_endpoint(floor: ConversationFloor, event: FloorEvent) -> list[FloorCommand]:
    text = event.text.strip()
    if not text:
        return _commands(FloorCommand(FloorCommandKind.EMIT_NOTICE, notice_category="not_understood"))
    floor.pending_transcript = text
    floor.state = FloorState.THINKING
    floor.in_flight_transcript = text
    mic = FloorCommand(FloorCommandKind.MIC_MUTE) if not floor.allow_duplex_capture else FloorCommand(
        FloorCommandKind.MIC_OPEN
    )
    return _commands(mic, FloorCommand(FloorCommandKind.START_THINK, text=text))


def _on_thinking_stt_endpoint(floor: ConversationFloor, event: FloorEvent) -> list[FloorCommand]:
    merged = " ".join(part for part in (floor.in_flight_transcript, event.text.strip()) if part).strip()
    floor.in_flight_transcript = merged
    floor.state = FloorState.THINKING
    return _commands(
        FloorCommand(FloorCommandKind.CANCEL_AGENT),
        FloorCommand(FloorCommandKind.STEER_THINK, text=merged),
    )


def _on_thinking_agent_first_text(floor: ConversationFloor, _event: FloorEvent) -> list[FloorCommand]:
    floor.state = FloorState.SPEAKING
    return []


def _on_thinking_agent_done(floor: ConversationFloor, _event: FloorEvent) -> list[FloorCommand]:
    floor.state = FloorState.IDLE
    floor.in_flight_transcript = ""
    floor.pending_transcript = ""
    return _commands(FloorCommand(FloorCommandKind.MIC_OPEN))


def _on_thinking_agent_error(floor: ConversationFloor, _event: FloorEvent) -> list[FloorCommand]:
    floor.state = FloorState.IDLE
    floor.in_flight_transcript = ""
    return _commands(
        FloorCommand(FloorCommandKind.EMIT_NOTICE, notice_category="error"),
        FloorCommand(FloorCommandKind.MIC_OPEN),
    )


def _on_speaking_barge_in(floor: ConversationFloor, event: FloorEvent) -> list[FloorCommand]:
    floor.state = FloorState.USER_SPEAKING
    floor.pending_transcript = event.text.strip()
    return _commands(
        FloorCommand(FloorCommandKind.CANCEL_TTS),
        FloorCommand(FloorCommandKind.CANCEL_AGENT),
        FloorCommand(FloorCommandKind.MARK_INTERRUPTED),
        FloorCommand(FloorCommandKind.MIC_OPEN),
    )


def _on_speaking_playback_stopped(floor: ConversationFloor, _event: FloorEvent) -> list[FloorCommand]:
    floor.state = FloorState.IDLE
    floor.in_flight_transcript = ""
    floor.pending_transcript = ""
    mic = FloorCommand(FloorCommandKind.MIC_OPEN)
    if not floor.allow_duplex_capture:
        return _commands(mic)
    return _commands(mic)


def _on_any_stt_lost(floor: ConversationFloor, _event: FloorEvent) -> list[FloorCommand]:
    floor.state = FloorState.EARS_DOWN
    return _commands(FloorCommand(FloorCommandKind.EMIT_NOTICE, notice_category="ears_offline"))


def _on_ears_down_restored(floor: ConversationFloor, _event: FloorEvent) -> list[FloorCommand]:
    floor.state = FloorState.IDLE
    return _commands(FloorCommand(FloorCommandKind.EMIT_NOTICE, notice_category="back_online"))


def _on_any_stop(floor: ConversationFloor, _event: FloorEvent) -> list[FloorCommand]:
    floor.state = FloorState.IDLE
    return _commands(
        FloorCommand(FloorCommandKind.CANCEL_TTS),
        FloorCommand(FloorCommandKind.CANCEL_AGENT),
        FloorCommand(FloorCommandKind.MIC_OPEN),
    )


_HANDLERS = {
    (FloorState.IDLE, FloorEventKind.STT_ENDPOINT): _on_idle_stt_endpoint,
    (FloorState.THINKING, FloorEventKind.STT_ENDPOINT): _on_thinking_stt_endpoint,
    (FloorState.THINKING, FloorEventKind.AGENT_FIRST_TEXT): _on_thinking_agent_first_text,
    (FloorState.THINKING, FloorEventKind.AGENT_DONE): _on_thinking_agent_done,
    (FloorState.THINKING, FloorEventKind.AGENT_ERROR): _on_thinking_agent_error,
    (FloorState.SPEAKING, FloorEventKind.BARGE_IN_CONFIRMED): _on_speaking_barge_in,
    (FloorState.SPEAKING, FloorEventKind.PLAYBACK_STOPPED): _on_speaking_playback_stopped,
    (FloorState.IDLE, FloorEventKind.STT_SESSION_LOST): _on_any_stt_lost,
    (FloorState.USER_SPEAKING, FloorEventKind.STT_SESSION_LOST): _on_any_stt_lost,
    (FloorState.AGGREGATING, FloorEventKind.STT_SESSION_LOST): _on_any_stt_lost,
    (FloorState.THINKING, FloorEventKind.STT_SESSION_LOST): _on_any_stt_lost,
    (FloorState.SPEAKING, FloorEventKind.STT_SESSION_LOST): _on_any_stt_lost,
    (FloorState.EARS_DOWN, FloorEventKind.STT_SESSION_RESTORED): _on_ears_down_restored,
    (FloorState.IDLE, FloorEventKind.STOP): _on_any_stop,
    (FloorState.USER_SPEAKING, FloorEventKind.STOP): _on_any_stop,
    (FloorState.AGGREGATING, FloorEventKind.STOP): _on_any_stop,
    (FloorState.THINKING, FloorEventKind.STOP): _on_any_stop,
    (FloorState.SPEAKING, FloorEventKind.STOP): _on_any_stop,
    (FloorState.EARS_DOWN, FloorEventKind.STOP): _on_any_stop,
}
