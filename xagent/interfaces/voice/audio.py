"""PCM conversion helpers for WebSocket voice audio."""
from __future__ import annotations

from array import array


class _PCMInputConverter:
    def __init__(self, *, source_channels: int, source_rate: int, target_channels: int, target_rate: int) -> None:
        self.source_channels = int(source_channels)
        self.source_rate = int(source_rate)
        self.target_channels = int(target_channels)
        self.target_rate = int(target_rate)
        self._resampler = _PCMResampler(channels=self.target_channels)

    def convert(self, chunk: bytes) -> bytes:
        if not chunk:
            return chunk
        converted = _convert_channels(chunk, self.source_channels, self.target_channels)
        if self.source_rate != self.target_rate:
            converted = self._resampler.resample(
                converted,
                source_rate=self.source_rate,
                target_rate=self.target_rate,
            )
        return converted


class _PCMOutputConverter:
    def __init__(self, *, source_channels: int, source_rate: int, target_channels: int, target_rate: int) -> None:
        self.source_channels = int(source_channels)
        self.source_rate = int(source_rate)
        self.target_channels = int(target_channels)
        self.target_rate = int(target_rate)
        self._resampler = _PCMResampler(channels=self.source_channels)

    def convert(self, chunk: bytes) -> bytes:
        if not chunk:
            return chunk
        converted = chunk
        if self.source_rate != self.target_rate:
            converted = self._resampler.resample(
                converted,
                source_rate=self.source_rate,
                target_rate=self.target_rate,
            )
        converted = _convert_channels(converted, self.source_channels, self.target_channels)
        return converted

    def flush(self) -> bytes:
        """Release interpolation lookahead at the end of one finite output stream."""
        if self.source_rate == self.target_rate or self._resampler._last_frame is None:
            return b""
        tail = self._resampler.resample(
            _samples_to_bytes(self._resampler._last_frame),
            source_rate=self.source_rate, target_rate=self.target_rate,
        )
        self._resampler = _PCMResampler(channels=self.source_channels)
        return _convert_channels(tail, self.source_channels, self.target_channels)


class _PCMResampler:
    def __init__(self, *, channels: int) -> None:
        self.channels = int(channels)
        self._position = 0.0
        self._last_frame: tuple[int, ...] | None = None

    def resample(self, chunk: bytes, *, source_rate: int, target_rate: int) -> bytes:
        if source_rate == target_rate or not chunk:
            return chunk
        samples = _samples_from_bytes(chunk)
        if not samples:
            return b""
        frames = _chunk_frames(samples, self.channels)
        if self._last_frame is not None:
            frames = [self._last_frame, *frames]
        if len(frames) < 2:
            self._last_frame = frames[-1]
            return b""
        step = float(source_rate) / float(target_rate)
        output: list[int] = []
        while self._position + 1 < len(frames):
            index = int(self._position)
            frac = self._position - index
            frame_a = frames[index]
            frame_b = frames[index + 1]
            for channel in range(self.channels):
                sample = round(frame_a[channel] + (frame_b[channel] - frame_a[channel]) * frac)
                output.append(_clamp_pcm16(sample))
            self._position += step
        self._position -= len(frames) - 1
        self._last_frame = frames[-1]
        return _samples_to_bytes(output)


def _convert_channels(chunk: bytes, source_channels: int, target_channels: int) -> bytes:
    if source_channels == target_channels or not chunk:
        return chunk
    samples = _samples_from_bytes(chunk)
    frames = _chunk_frames(samples, source_channels)
    converted: list[int] = []
    if source_channels == 2 and target_channels == 1:
        for left, right in frames:
            converted.append(_clamp_pcm16(round((left + right) / 2)))
        return _samples_to_bytes(converted)
    if source_channels == 1 and target_channels == 2:
        for (mono,) in frames:
            converted.extend((mono, mono))
        return _samples_to_bytes(converted)
    raise RuntimeError(f"Unsupported channel conversion: {source_channels} -> {target_channels}")


def _samples_from_bytes(chunk: bytes) -> list[int]:
    samples = array("h")
    samples.frombytes(chunk)
    return samples.tolist()


def _samples_to_bytes(samples: list[int]) -> bytes:
    if not samples:
        return b""
    encoded = array("h", (_clamp_pcm16(sample) for sample in samples))
    return encoded.tobytes()


def _chunk_frames(samples: list[int], channels: int) -> list[tuple[int, ...]]:
    if len(samples) % channels != 0:
        raise RuntimeError(f"PCM chunk does not align to {channels} channel frame boundaries")
    return [tuple(samples[index:index + channels]) for index in range(0, len(samples), channels)]


def _clamp_pcm16(value: int) -> int:
    return max(-32768, min(32767, int(value)))
