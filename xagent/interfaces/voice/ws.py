"""WebSocket audio transport adapters for paired remote voice devices."""
from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, replace
from typing import Annotated, Any, Callable, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, TypeAdapter

from .audio import _PCMInputConverter, _PCMOutputConverter
from .transport_audio import AudioCodecError, AudioPacket, OpusDecoder, OpusEncoder, ensure_opus_ready
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


class AudioInputBackpressure(ValueError):
    """The recognition consumer is not keeping up, not an invalid client frame."""


class VoiceSessionHello(BaseModel):
    """One explicit audio contract for browser and device clients."""
    model_config = ConfigDict(extra="forbid")
    type: Literal["hello"]
    audio_protocol: Literal[2]
    codec: Literal["opus", "pcm_s16le"]
    profile: VoiceProfileName = "room"
    echo_managed: StrictBool = True
    interruptions: StrictBool | None = None
    user_id: str | None = Field(default=None, max_length=128)
    name: str = Field(default="", max_length=256)
    location: str = Field(default="", max_length=256)

    @property
    def input_sample_rate(self) -> int:
        return 48000 if self.codec == 'opus' else 16000

    @property
    def output_sample_rate(self) -> int:
        return 48000 if self.codec == 'opus' else 24000

    @property
    def input_channels(self) -> int:
        return 1

    @property
    def output_channels(self) -> int:
        return 1


class _Control(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InputControl(_Control):
    type: Literal['input_start', 'input_end']
    stream_id: int = Field(gt=0, le=0xffffffff)


class PlaybackProgress(_Control):
    type: Literal['playback_progress']
    playback_id: str = Field(min_length=1, max_length=64)
    played_samples: int = Field(ge=0)
    sample_rate: Literal[24000, 48000]


class PlaybackFinished(_Control):
    type: Literal['playback_finished']
    playback_id: str = Field(min_length=1, max_length=64)


class SessionControl(_Control):
    type: Literal['ping', 'stop']


_CLIENT_CONTROL = TypeAdapter(Annotated[InputControl | PlaybackProgress | PlaybackFinished | SessionControl, Field(discriminator='type')])


def parse_client_control(raw: str):
    return _CLIENT_CONTROL.validate_python(parse_voice_control_frame(raw))


@dataclass
class WebSocketVoiceBridge:
    """Thread-safe bridge between a FastAPI WebSocket and synchronous VoiceRuntime I/O."""

    websocket: Any
    loop: asyncio.AbstractEventLoop
    hello: VoiceSessionHello
    room_name: str
    send_lock: asyncio.Lock
    closed: threading.Event
    def __post_init__(self):
        ensure_opus_ready()

    def codec_failed(self) -> None:
        self.send_json({'type': 'error', 'code': 'audio_codec', 'message': 'Audio codec failed. End the conversation and retry.'})
        async def close_failed():
            await self.websocket.close(code=1011, reason='Audio codec failed')
        self._schedule_send(close_failed)
        self.closed.set()

    def recognition_unavailable(self) -> None:
        async def close_unavailable():
            await self.websocket.close(code=1013, reason='Speech recognition disconnected. Reconnecting.')
        self._schedule_send(close_unavailable)
        self.closed.set()

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
        self._chunks: "queue.Queue[AudioPacket | tuple[str, int] | None]" = queue.Queue(maxsize=64)
        self._closed = False
        self._stream = 0
        self._accepting_input = False
        self._sequence = 0
        self._samples = 0
        self._wire_bytes = 0
        self._queued_samples = 0
        self._queue_lock = threading.Lock()
        self.consuming = threading.Event()
        self._last_consumed_at: float | None = None

    def start_input(self, stream_id: int) -> None:
        if stream_id <= self._stream:
            raise ValueError('Input stream IDs must increase')
        self._stream = stream_id
        self._accepting_input = True
        self._sequence = self._samples = self._wire_bytes = 0
        self._put(('start', stream_id))

    def end_input(self, stream_id: int) -> None:
        if stream_id == self._stream:
            self._accepting_input = False
            logger.info('Voice capture codec=%s audio_ms=%.1f wire_bytes=%d queue_ms=%.1f',
                        self.bridge.hello.codec, self._samples * 1000 / self.stream_sample_rate,
                        self._wire_bytes, self._queued_samples * 1000 / self.stream_sample_rate)

    def _put(self, item) -> None:
        try:
            self._chunks.put_nowait(item)
        except queue.Full as exc:
            raise AudioInputBackpressure('Audio input queue exceeded 500 ms') from exc

    def push_audio(self, data: bytes) -> None:
        if self._closed or not data:
            return
        packet = AudioPacket.unpack(data)
        if packet.samples + packet.discard > self.stream_sample_rate // 50:
            raise ValueError('Audio packet exceeds the fixed 20 ms frame size')
        if packet.stream != self._stream or not self._accepting_input:
            return
        if packet.sequence != self._sequence or abs(packet.timestamp - self._samples * 1_000_000 // self.stream_sample_rate) > 1:
            raise ValueError('Discontinuous input audio')
        with self._queue_lock:
            if self._queued_samples + packet.samples > self.stream_sample_rate // 2:
                logger.warning('Voice input overload codec=%s stream=%d queue_ms=%.1f packets=%d consumer_idle_ms=%s',
                               self.bridge.hello.codec, self._stream, self._queued_samples * 1000 / self.stream_sample_rate,
                               self._chunks.qsize(), None if self._last_consumed_at is None else round((time.monotonic() - self._last_consumed_at) * 1000))
                raise AudioInputBackpressure('Audio input queue exceeded 500 ms')
            self._put(packet)
            self._queued_samples += packet.samples
        self._sequence += 1
        self._samples += packet.samples
        self._wire_bytes += len(data)

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
        # First pull comes from the STT sender, after its connection has opened.
        # Merely constructing this generator does not mean it can consume audio.
        self.consuming.set()
        converter = _PCMInputConverter(
            source_channels=self.stream_channels,
            source_rate=self.stream_sample_rate,
            target_channels=self.channels,
            target_rate=self.sample_rate,
        )
        decoder = None
        while not stop_event.is_set() and not self.bridge.closed.is_set():
            try:
                raw_chunk = self._chunks.get(timeout=0.2)
            except queue.Empty:
                yield b""
                continue
            if raw_chunk is None:
                return
            if isinstance(raw_chunk, tuple):
                try:
                    decoder = OpusDecoder() if self.bridge.hello.codec == 'opus' else None
                except Exception as exc:
                    self.bridge.codec_failed()
                    raise AudioCodecError('Opus input initialization failed') from exc
                converter = _PCMInputConverter(source_channels=1, source_rate=self.stream_sample_rate,
                                               target_channels=self.channels, target_rate=self.sample_rate)
                continue
            if isinstance(raw_chunk, AudioPacket):
                with self._queue_lock:
                    self._queued_samples -= raw_chunk.samples
                    self._last_consumed_at = time.monotonic()
                if self.bridge.hello.codec == 'opus':
                    try:
                        raw_chunk = decoder.decode(raw_chunk)
                    except Exception as exc:
                        self.bridge.codec_failed()
                        raise AudioCodecError('Opus input failed') from exc
                else:
                    if raw_chunk.discard or len(raw_chunk.payload) != raw_chunk.samples * 2:
                        raise ValueError('Invalid PCM packet length')
                    raw_chunk = raw_chunk.payload
            if pause_event.is_set() or stop_event.is_set():
                continue
            yield converter.convert(raw_chunk)


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
        self._playback_lock = threading.Lock()
        self._playback_id: str | None = None
        self._finished = threading.Event()
        self._stream = 0
        self.sent_samples = 0

    def _audio_packets(self, chunks, stop_event):
        try:
            encoder = OpusEncoder() if self.bridge.hello.codec == 'opus' else None
        except Exception as exc:
            raise AudioCodecError('Opus output initialization failed') from exc
        converter = _PCMOutputConverter(source_channels=self.channels, source_rate=self.sample_rate,
                                       target_channels=self.stream_channels, target_rate=self.stream_sample_rate)
        buffer = bytearray()
        sequence = samples = 0
        size = self.stream_sample_rate // 50 * 2
        for chunk in chunks:
            if stop_event.is_set() or self.bridge.closed.is_set():
                return
            pcm = converter.convert(chunk)
            if encoder:
                yield from encoder.push(pcm)
            else:
                buffer.extend(pcm)
                while len(buffer) >= size:
                    yield AudioPacket(1, sequence, samples * 1_000_000 // self.stream_sample_rate, size // 2, 0, bytes(buffer[:size]))
                    del buffer[:size]
                    sequence += 1
                    samples += size // 2
        if encoder:
            yield from encoder.push(converter.flush())
            yield from encoder.finish()
        elif buffer:
            yield AudioPacket(1, sequence, samples * 1_000_000 // self.stream_sample_rate, len(buffer) // 2, 0, bytes(buffer))

    def accepts_playback(self, playback_id: str | None) -> bool:
        with self._playback_lock:
            return playback_id is not None and playback_id == self._playback_id

    def note_playback_finished(self, playback_id: str | None) -> None:
        with self._playback_lock:
            if playback_id is not None and playback_id == self._playback_id:
                self._finished.set()

    def close(self) -> None:
        self.bridge.closed.set()

    def play_chunks(self, chunks: Iterator[bytes], *, stop_event: threading.Event) -> None:
        playback_id = uuid.uuid4().hex
        with self._playback_lock:
            self._playback_id = playback_id
            self._finished.clear()
        identity = {"playback_id": playback_id}
        self._stream += 1
        self.sent_samples = 0
        media_started = None
        sent_bytes = 0
        identity.update(stream_id=self._stream, codec=self.bridge.hello.codec)
        try:
            self.bridge.send_json(
                {
                    "type": "playback_start",
                    **identity,
                    "sample_rate": self.stream_sample_rate,
                    "channels": self.stream_channels,
                }
            )
            interrupted = False
            outgoing = self._audio_packets(chunks, stop_event)
            for chunk in outgoing:
                if stop_event.is_set() or self.bridge.closed.is_set():
                    interrupted = stop_event.is_set()
                    break
                if media_started is None:
                    media_started = time.monotonic()
                # Pace media at most 200 ms ahead of the audio clock, keeping
                # network and browser decoder queues bounded even for fast TTS.
                while self.sent_samples / self.stream_sample_rate - (time.monotonic() - media_started) > 0.2:
                    if stop_event.is_set() or self.bridge.closed.wait(0.02):
                        interrupted = stop_event.is_set()
                        break
                if interrupted or self.bridge.closed.is_set():
                    break
                wire = replace(chunk, stream=self._stream).pack()
                self.sent_samples += chunk.samples
                self.bridge.send_bytes(wire)
                sent_bytes += len(wire)
                if stop_event.is_set() or self.bridge.closed.is_set():
                    interrupted = stop_event.is_set()
                    break
            if not interrupted and not self.bridge.closed.is_set():
                self.bridge.send_json({"type": "playback_done", **identity})
                duration = self.sent_samples / self.stream_sample_rate
                deadline = time.monotonic() + duration + 5.0
                while not self._finished.wait(0.05):
                    if stop_event.is_set() or self.bridge.closed.is_set():
                        interrupted = stop_event.is_set()
                        break
                    if time.monotonic() >= deadline:
                        self.bridge.send_json({"type": "error", "code": "playback_timeout", "message": "Playback confirmation timed out. Please try again."})
                        async def close_timed_out():
                            await self.bridge.websocket.close(code=1011, reason="Playback confirmation timed out. Please try again.")
                        self.bridge._schedule_send(close_timed_out)
                        self.bridge.closed.set()
                        raise TimeoutError("Browser playback confirmation timed out")
            if interrupted and not self.bridge.closed.is_set():
                self.bridge.send_json({"type": "clear_playback", **identity})
        except AudioCodecError:
            self.bridge.codec_failed()
            raise
        finally:
            logger.info('Voice playback codec=%s audio_ms=%.1f wire_bytes=%d',
                        self.bridge.hello.codec, self.sent_samples * 1000 / self.stream_sample_rate, sent_bytes)
            with self._playback_lock:
                self._playback_id = None


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
