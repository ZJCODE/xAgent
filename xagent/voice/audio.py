"""Local microphone and speaker adapters for the voice CLI."""
from __future__ import annotations

import logging
import queue
import threading
from array import array
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Iterator


logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class _AudioDeviceInfo:
    index: int
    name: str
    hostapi_name: str
    max_input_channels: int
    max_output_channels: int
    default_sample_rate: int
    is_default_input: bool
    is_default_output: bool


@dataclass(frozen=True)
class AudioStreamSelection:
    device_index: int | None
    device_name: str
    hostapi_name: str
    stream_channels: int
    stream_sample_rate: int
    target_channels: int
    target_sample_rate: int


@dataclass(frozen=True)
class AudioIOProfile:
    input_selection: AudioStreamSelection
    output_selection: AudioStreamSelection


def resolve_audio_io_profile(
    *,
    input_sample_rate: int,
    input_channels: int,
    output_sample_rate: int,
    output_channels: int,
    dtype: str = "int16",
) -> AudioIOProfile:
    sd = _import_sounddevice()
    devices = _query_audio_devices(sd)
    _log_audio_device_inventory(devices)
    input_selection = _select_input_device(
        sd,
        devices,
        desired_rate=input_sample_rate,
        desired_channels=input_channels,
        dtype=dtype,
    )
    output_selection = _select_output_device(
        sd,
        devices,
        desired_rate=output_sample_rate,
        desired_channels=output_channels,
        dtype=dtype,
        preferred_duplex_name=input_selection.device_name,
    )
    logger.info(
        "Voice audio input selected: device=%s hostapi=%s stream=%sch@%sHz target=%sch@%sHz",
        input_selection.device_name,
        input_selection.hostapi_name or "unknown",
        input_selection.stream_channels,
        input_selection.stream_sample_rate,
        input_selection.target_channels,
        input_selection.target_sample_rate,
    )
    logger.info(
        "Voice audio output selected: device=%s hostapi=%s stream=%sch@%sHz target=%sch@%sHz",
        output_selection.device_name,
        output_selection.hostapi_name or "unknown",
        output_selection.stream_channels,
        output_selection.stream_sample_rate,
        output_selection.target_channels,
        output_selection.target_sample_rate,
    )
    return AudioIOProfile(
        input_selection=input_selection,
        output_selection=output_selection,
    )


def _query_audio_devices(sd) -> list[_AudioDeviceInfo]:  # noqa: ANN001
    raw_devices = sd.query_devices()
    if isinstance(raw_devices, dict):
        raw_devices = [raw_devices]
    hostapi_names = _hostapi_names(sd)
    default_input, default_output = _default_device_indices(sd)
    devices: list[_AudioDeviceInfo] = []
    for index, raw in enumerate(raw_devices):
        info = dict(raw)
        devices.append(
            _AudioDeviceInfo(
                index=index,
                name=str(info.get("name") or f"device-{index}"),
                hostapi_name=hostapi_names.get(_coerce_int(info.get("hostapi")), ""),
                max_input_channels=int(info.get("max_input_channels", 0) or 0),
                max_output_channels=int(info.get("max_output_channels", 0) or 0),
                default_sample_rate=int(round(float(info.get("default_samplerate", 0) or 0))),
                is_default_input=index == default_input,
                is_default_output=index == default_output,
            )
        )
    return devices


def _hostapi_names(sd) -> dict[int, str]:  # noqa: ANN001
    try:
        raw_hostapis = sd.query_hostapis()
    except Exception:
        return {}
    if isinstance(raw_hostapis, dict):
        raw_hostapis = [raw_hostapis]
    names: dict[int, str] = {}
    for index, info in enumerate(raw_hostapis):
        data = dict(info)
        names[index] = str(data.get("name") or index)
    return names


def _default_device_indices(sd) -> tuple[int | None, int | None]:  # noqa: ANN001
    try:
        raw_defaults = getattr(getattr(sd, "default", None), "device", None)
    except Exception:
        raw_defaults = None
    if raw_defaults is None:
        return (None, None)
    values = _coerce_default_pair(raw_defaults)
    if len(values) < 2:
        return (None, None)
    defaults: list[int | None] = []
    for value in values[:2]:
        normalized = _coerce_int(value)
        if normalized is None:
            defaults.append(None)
            continue
        defaults.append(normalized if normalized >= 0 else None)
    return (defaults[0], defaults[1])


def _log_audio_device_inventory(devices: list[_AudioDeviceInfo]) -> None:
    if not logger.isEnabledFor(logging.INFO):
        return
    if not devices:
        logger.info("No local audio devices reported by sounddevice")
        return
    default_inputs = [device.name for device in devices if device.is_default_input]
    default_outputs = [device.name for device in devices if device.is_default_output]
    logger.info(
        "Default audio devices: input=%s output=%s",
        ", ".join(default_inputs) or "none",
        ", ".join(default_outputs) or "none",
    )
    logger.info("Detected %s local audio device(s):", len(devices))
    for device in devices:
        flags: list[str] = []
        if device.is_default_input:
            flags.append("default-input")
        if device.is_default_output:
            flags.append("default-output")
        flag_suffix = f" [{' '.join(flags)}]" if flags else ""
        logger.info(
            "  #%s %s%s hostapi=%s in=%s out=%s default-rate=%sHz",
            device.index,
            device.name,
            flag_suffix,
            device.hostapi_name or "unknown",
            device.max_input_channels,
            device.max_output_channels,
            device.default_sample_rate,
        )


def _select_input_device(
    sd,  # noqa: ANN001
    devices: list[_AudioDeviceInfo],
    *,
    desired_rate: int,
    desired_channels: int,
    dtype: str,
) -> AudioStreamSelection:
    candidates: list[tuple[int, AudioStreamSelection]] = []
    for device in devices:
        if device.max_input_channels <= 0:
            continue
        for channels in _candidate_channel_counts(device.max_input_channels, desired_channels):
            for sample_rate in _candidate_sample_rates(device.default_sample_rate, desired_rate):
                if not _supports_input_settings(sd, device.index, channels, sample_rate, dtype):
                    continue
                score = 0
                if device.is_default_input:
                    score += 200
                if channels == desired_channels:
                    score += 120
                elif channels == 2:
                    score += 90
                if sample_rate == desired_rate:
                    score += 80
                elif sample_rate == device.default_sample_rate:
                    score += 20
                score -= abs(sample_rate - desired_rate) // 1000
                candidates.append(
                    (
                        score,
                        AudioStreamSelection(
                            device_index=device.index,
                            device_name=device.name,
                            hostapi_name=device.hostapi_name,
                            stream_channels=channels,
                            stream_sample_rate=sample_rate,
                            target_channels=desired_channels,
                            target_sample_rate=desired_rate,
                        ),
                    )
                )
    if not candidates:
        raise RuntimeError(
            f"No compatible input device found for {desired_channels}ch/{desired_rate}Hz capture"
        )
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _select_output_device(
    sd,  # noqa: ANN001
    devices: list[_AudioDeviceInfo],
    *,
    desired_rate: int,
    desired_channels: int,
    dtype: str,
    preferred_duplex_name: str,
) -> AudioStreamSelection:
    preferred_name = _normalize_device_name(preferred_duplex_name)
    candidates: list[tuple[int, AudioStreamSelection]] = []
    for device in devices:
        if device.max_output_channels <= 0:
            continue
        for channels in _candidate_channel_counts(device.max_output_channels, desired_channels):
            for sample_rate in _candidate_sample_rates(device.default_sample_rate, desired_rate):
                if not _supports_output_settings(sd, device.index, channels, sample_rate, dtype):
                    continue
                score = 0
                if device.is_default_output:
                    score += 200
                if _normalize_device_name(device.name) == preferred_name:
                    score += 260
                if channels == desired_channels:
                    score += 120
                elif channels == 2:
                    score += 100
                if sample_rate == desired_rate:
                    score += 80
                elif sample_rate == device.default_sample_rate:
                    score += 20
                score -= abs(sample_rate - desired_rate) // 1000
                candidates.append(
                    (
                        score,
                        AudioStreamSelection(
                            device_index=device.index,
                            device_name=device.name,
                            hostapi_name=device.hostapi_name,
                            stream_channels=channels,
                            stream_sample_rate=sample_rate,
                            target_channels=desired_channels,
                            target_sample_rate=desired_rate,
                        ),
                    )
                )
    if not candidates:
        raise RuntimeError(
            f"No compatible output device found for {desired_channels}ch/{desired_rate}Hz playback"
        )
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _candidate_channel_counts(max_channels: int, desired_channels: int) -> list[int]:
    options: list[int] = []
    for value in (desired_channels, 2, 1, min(max_channels, 2)):
        if value <= 0 or value > max_channels or value in options:
            continue
        options.append(value)
    return options or [max_channels]


def _candidate_sample_rates(default_rate: int, desired_rate: int) -> list[int]:
    options: list[int] = []
    for value in (desired_rate, default_rate, 48000, 44100, 32000, 24000, 16000, 8000):
        if value <= 0 or value in options:
            continue
        options.append(int(value))
    return options


def _supports_input_settings(sd, device: int, channels: int, sample_rate: int, dtype: str) -> bool:  # noqa: ANN001
    checker = getattr(sd, "check_input_settings", None)
    if checker is None:
        return True
    try:
        checker(device=device, channels=channels, samplerate=sample_rate, dtype=dtype)
        return True
    except Exception:
        return False


def _supports_output_settings(sd, device: int, channels: int, sample_rate: int, dtype: str) -> bool:  # noqa: ANN001
    checker = getattr(sd, "check_output_settings", None)
    if checker is None:
        return True
    try:
        checker(device=device, channels=channels, samplerate=sample_rate, dtype=dtype)
        return True
    except Exception:
        return False


def _normalize_device_name(value: str) -> str:
    return " ".join(str(value or "").lower().split())


def _coerce_default_pair(raw_defaults: Any) -> list[Any]:
    values: list[Any] = []
    for key in (0, 1, "input", "output"):
        try:
            value = raw_defaults[key]
        except Exception:
            continue
        values.append(value)
        if len(values) == 2:
            return values
    try:
        if isinstance(raw_defaults, Iterable):
            values = list(raw_defaults)
    except TypeError:
        values = []
    if values:
        return values
    return [raw_defaults]


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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


class SoundDeviceMicrophone:
    """Yield raw PCM microphone chunks suitable for Soniox realtime STT."""

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        channels: int = 1,
        block_ms: int = 120,
        dtype: str = "int16",
        device_index: int | None = None,
        device_name: str = "default",
        stream_sample_rate: int | None = None,
        stream_channels: int | None = None,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.block_ms = int(block_ms)
        self.dtype = dtype
        self.device_index = device_index
        self.device_name = device_name
        self.stream_sample_rate = int(stream_sample_rate or sample_rate)
        self.stream_channels = int(stream_channels or channels)

    @property
    def block_frames(self) -> int:
        return max(1, int(self.stream_sample_rate * self.block_ms / 1000))

    def iter_chunks(
        self,
        *,
        pause_event: threading.Event,
        stop_event: threading.Event,
    ) -> Iterator[bytes]:
        sd = _import_sounddevice()
        chunks: "queue.Queue[bytes]" = queue.Queue(maxsize=32)
        converter = _PCMInputConverter(
            source_channels=self.stream_channels,
            source_rate=self.stream_sample_rate,
            target_channels=self.channels,
            target_rate=self.sample_rate,
        )

        logger.info(
            "Opening microphone stream: device=%s stream=%sch@%sHz target=%sch@%sHz block=%s frames",
            self.device_name,
            self.stream_channels,
            self.stream_sample_rate,
            self.channels,
            self.sample_rate,
            self.block_frames,
        )

        def callback(indata, frames, time_info, status) -> None:  # noqa: ANN001
            del frames, time_info
            if status:
                logger.info("Microphone stream status from %s: %s", self.device_name, status)
            if stop_event.is_set() or pause_event.is_set():
                return
            try:
                chunks.put_nowait(bytes(indata))
            except queue.Full:
                pass

        with sd.RawInputStream(
            device=self.device_index,
            samplerate=self.stream_sample_rate,
            channels=self.stream_channels,
            dtype=self.dtype,
            blocksize=self.block_frames,
            callback=callback,
        ):
            while not stop_event.is_set():
                try:
                    raw_chunk = chunks.get(timeout=0.2)
                    yield converter.convert(raw_chunk)
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
        device_index: int | None = None,
        device_name: str = "default",
        stream_sample_rate: int | None = None,
        stream_channels: int | None = None,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.dtype = dtype
        self.device_index = device_index
        self.device_name = device_name
        self.stream_sample_rate = int(stream_sample_rate or sample_rate)
        self.stream_channels = int(stream_channels or channels)

    def play_chunks(self, chunks: Iterator[bytes], *, stop_event: threading.Event) -> None:
        sd = _import_sounddevice()
        converter = _PCMOutputConverter(
            source_channels=self.channels,
            source_rate=self.sample_rate,
            target_channels=self.stream_channels,
            target_rate=self.stream_sample_rate,
        )
        logger.info(
            "Opening speaker stream: device=%s stream=%sch@%sHz source=%sch@%sHz",
            self.device_name,
            self.stream_channels,
            self.stream_sample_rate,
            self.channels,
            self.sample_rate,
        )
        with sd.RawOutputStream(
            device=self.device_index,
            samplerate=self.stream_sample_rate,
            channels=self.stream_channels,
            dtype=self.dtype,
        ) as stream:
            for chunk in chunks:
                if stop_event.is_set():
                    stream.abort()
                    break
                if chunk:
                    stream.write(converter.convert(chunk))
                    if stop_event.is_set():
                        stream.abort()
                        break
