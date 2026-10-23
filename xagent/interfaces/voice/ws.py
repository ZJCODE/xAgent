"""WebSocket audio transport adapters for paired remote voice devices."""
from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .audio import _PCMInputConverter, _PCMOutputConverter
from .config import (
    SONIOX_STT_CHANNELS,
    SONIOX_STT_SAMPLE_RATE,
    SONIOX_TTS_CHANNELS,
    SONIOX_TTS_SAMPLE_RATE,
    VoiceProfileName,
)

logger = logging.getLogger(__name__)

MAX_VOICE_CONTROL_BYTES = 16 * 1024
MAX_VOICE_AUDIO_CHUNK_BYTES = 256 * 1024
# Soniox closes an active STT session that receives no audio for about 20s.
# Push-to-talk sends nothing between utterances, so idle gaps must be silence.
_IDLE_SILENCE_SECONDS = 0.2


class VoiceSessionHello(BaseModel):
    """Initial handshake message sent by a remote device opening a voice stream."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["hello"] = "hello"
    input_sample_rate: int = Field(default=SONIOX_STT_SAMPLE_RATE, ge=8_000, le=48_000)
    input_channels: int = Field(default=SONIOX_STT_CHANNELS, ge=1, le=2)
    output_sample_rate: int = Field(default=SONIOX_TTS_SAMPLE_RATE, ge=8_000, le=48_000)
    output_channels: int = Field(default=SONIOX_TTS_CHANNELS, ge=1, le=2)
    profile: VoiceProfileName = "room"
    echo_managed: StrictBool = True
    interruptions: StrictBool | None = None
    user_id: str | None = Field(default=None, max_length=128)
    name: str = Field(default="", max_length=256)
    location: str = Field(default="", max_length=256)


class VoiceClientControl(BaseModel):
    """Runtime control frame sent from the device during an active voice session."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["playback_progress", "ping", "stop"]
    played_bytes: int = Field(default=0, ge=0)


@dataclass
class WebSocketVoiceBridge:
    """Thread-safe bridge between a FastAPI WebSocket and synchronous VoiceRuntime I/O."""

    websocket: Any
    loop: asyncio.AbstractEventLoop
    hello: VoiceSessionHello
    room_name: str
    send_lock: asyncio.Lock
    closed: threading.Event

    def _schedule_send(self, work) -> None:
        if self.closed.is_set():
            return
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop is self.loop:
            task = self.loop.create_task(work())
            task.add_done_callback(self._on_task_done)
            return

        try:
            future = asyncio.run_coroutine_threadsafe(work(), self.loop)
            future.result(timeout=5.0)
        except Exception:
            self.closed.set()

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        if task.exception() is not None:
            self.closed.set()

    def send_json(self, payload: dict[str, Any]) -> None:
        async def _send() -> None:
            if self.closed.is_set():
                return
            async with self.send_lock:
                await self.websocket.send_json(payload)

        self._schedule_send(_send)

    def send_bytes(self, data: bytes) -> None:
        if not data:
            return

        async def _send() -> None:
            if self.closed.is_set():
                return
            async with self.send_lock:
                await self.websocket.send_bytes(data)

        self._schedule_send(_send)


class WebSocketMicrophone:
    """Yield 16kHz mono PCM chunks received over a device WebSocket."""

    def __init__(
        self,
        bridge: WebSocketVoiceBridge,
        *,
        sample_rate: int = SONIOX_STT_SAMPLE_RATE,
        channels: int = SONIOX_STT_CHANNELS,
    ) -> None:
        self.bridge = bridge
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.stream_sample_rate = int(bridge.hello.input_sample_rate)
        self.stream_channels = int(bridge.hello.input_channels)
        self._chunks: "queue.Queue[bytes | None]" = queue.Queue(maxsize=64)
        self._closed = False

    def push_audio(self, data: bytes) -> None:
        if self._closed or not data:
            return
        try:
            self._chunks.put_nowait(bytes(data))
        except queue.Full:
            try:
                self._chunks.get_nowait()
            except queue.Empty:
                pass
            try:
                self._chunks.put_nowait(bytes(data))
            except queue.Full:
                pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.bridge.closed.set()
        try:
            self._chunks.put_nowait(None)
        except queue.Full:
            try:
                self._chunks.get_nowait()
            except queue.Empty:
                pass
            try:
                self._chunks.put_nowait(None)
            except queue.Full:
                pass

    def iter_chunks(
        self,
        *,
        pause_event: threading.Event,
        stop_event: threading.Event,
    ) -> Iterator[bytes]:
        converter = _PCMInputConverter(
            source_channels=self.stream_channels,
            source_rate=self.stream_sample_rate,
            target_channels=self.channels,
            target_rate=self.sample_rate,
        )
        while not stop_event.is_set() and not self.bridge.closed.is_set():
            try:
                raw_chunk = self._chunks.get(timeout=_IDLE_SILENCE_SECONDS)
            except queue.Empty:
                if pause_event.is_set() or stop_event.is_set():
                    yield b""
                    continue
                yield converter.convert(self._idle_silence())
                continue
            if raw_chunk is None:
                return
            if pause_event.is_set() or stop_event.is_set():
                continue
            yield converter.convert(raw_chunk)

    def _idle_silence(self) -> bytes:
        frames = max(1, int(self.stream_sample_rate * _IDLE_SILENCE_SECONDS))
        return b"\x00\x00" * frames * max(1, self.stream_channels)


class WebSocketPlayer:
    """Stream synthesized TTS PCM chunks over a device WebSocket."""

    tracks_remote_playback_progress = True

    def __init__(
        self,
        bridge: WebSocketVoiceBridge,
        *,
        sample_rate: int = SONIOX_TTS_SAMPLE_RATE,
        channels: int = SONIOX_TTS_CHANNELS,
    ) -> None:
        self.bridge = bridge
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.stream_sample_rate = int(bridge.hello.output_sample_rate)
        self.stream_channels = int(bridge.hello.output_channels)
        self._converter = _PCMOutputConverter(
            source_channels=self.channels,
            source_rate=self.sample_rate,
            target_channels=self.stream_channels,
            target_rate=self.stream_sample_rate,
        )

    def close(self) -> None:
        self.bridge.closed.set()

    def play_chunks(self, chunks: Iterator[bytes], *, stop_event: threading.Event) -> None:
        self.bridge.send_json(
            {
                "type": "playback_start",
                "sample_rate": self.stream_sample_rate,
                "channels": self.stream_channels,
            }
        )
        interrupted = False
        for chunk in chunks:
            if stop_event.is_set() or self.bridge.closed.is_set():
                interrupted = stop_event.is_set()
                break
            if chunk:
                converted = self._converter.convert(chunk)
                if converted:
                    self.bridge.send_bytes(converted)
                if stop_event.is_set() or self.bridge.closed.is_set():
                    interrupted = stop_event.is_set()
                    break
        if interrupted and not self.bridge.closed.is_set():
            self.bridge.send_json({"type": "clear_playback"})
        elif not self.bridge.closed.is_set():
            self.bridge.send_json({"type": "playback_done"})


def make_websocket_output_sink(bridge: WebSocketVoiceBridge) -> Callable[..., None]:
    """Translate VoiceRuntime console output lines into structured WebSocket transcript events."""
    assistant_parts: list[str] = []

    def _output(*args: Any, end: str = "\n", **kwargs: Any) -> None:
        del kwargs
        text = " ".join(str(arg) for arg in args)
        if text == "Agent: " and end == "":
            assistant_parts.clear()
            bridge.send_json({"type": "state", "floor": "speaking"})
            return
        if assistant_parts is not None and end == "" and text:
            assistant_parts.append(text)
            bridge.send_json({"type": "transcript", "role": "assistant", "delta": text, "final": False})
            return
        if text == "" and end == "\n" and assistant_parts:
            full_text = "".join(assistant_parts).strip()
            assistant_parts.clear()
            if full_text:
                bridge.send_json({"type": "transcript", "role": "assistant", "text": full_text, "final": True})
            bridge.send_json({"type": "state", "floor": "listening"})
            return
        if text.startswith("User: "):
            utterance = text[len("User: ") :].strip()
            bridge.send_json({"type": "transcript", "role": "user", "text": utterance, "final": True})
            bridge.send_json({"type": "state", "floor": "thinking"})

    return _output


def parse_voice_control_frame(raw: str) -> dict[str, Any]:
    if len(raw.encode("utf-8")) > MAX_VOICE_CONTROL_BYTES:
        raise ValueError("Voice control message exceeds 16 KiB")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Voice control message must be a JSON object")
    return data
