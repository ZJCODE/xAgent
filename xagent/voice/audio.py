"""Local microphone and speaker adapters for the voice CLI."""
from __future__ import annotations

import queue
import threading
from typing import Iterator


class MissingVoiceDependencyError(RuntimeError):
    """Raised when bundled voice dependencies are not installed."""


def _import_sounddevice():
    try:
        import sounddevice as sd  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised by CLI import guard
        raise MissingVoiceDependencyError(
            "The voice command requires the local audio dependency sounddevice. "
            "Reinstall or upgrade myxagent, then try again."
        ) from exc
    return sd


class SoundDeviceMicrophone:
    """Yield raw PCM microphone chunks suitable for Soniox realtime STT."""

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        channels: int = 1,
        block_ms: int = 120,
        dtype: str = "int16",
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.block_ms = int(block_ms)
        self.dtype = dtype

    @property
    def block_frames(self) -> int:
        return max(1, int(self.sample_rate * self.block_ms / 1000))

    def iter_chunks(
        self,
        *,
        pause_event: threading.Event,
        stop_event: threading.Event,
    ) -> Iterator[bytes]:
        sd = _import_sounddevice()
        chunks: "queue.Queue[bytes]" = queue.Queue(maxsize=32)

        def callback(indata, frames, time_info, status) -> None:  # noqa: ANN001
            del frames, time_info, status
            if stop_event.is_set() or pause_event.is_set():
                return
            try:
                chunks.put_nowait(bytes(indata))
            except queue.Full:
                pass

        with sd.RawInputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype=self.dtype,
            blocksize=self.block_frames,
            callback=callback,
        ):
            while not stop_event.is_set():
                try:
                    yield chunks.get(timeout=0.2)
                except queue.Empty:
                    yield b""


class SoundDevicePlayer:
    """Play raw PCM chunks returned by Soniox realtime TTS."""

    def __init__(
        self,
        *,
        sample_rate: int = 24000,
        channels: int = 1,
        dtype: str = "int16",
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.dtype = dtype

    def play_chunks(self, chunks: Iterator[bytes], *, stop_event: threading.Event) -> None:
        sd = _import_sounddevice()
        with sd.RawOutputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype=self.dtype,
        ) as stream:
            for chunk in chunks:
                if stop_event.is_set():
                    break
                if chunk:
                    stream.write(chunk)
