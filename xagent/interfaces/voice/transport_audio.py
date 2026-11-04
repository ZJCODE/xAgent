"""Versioned WS audio packets and Opus codecs. Internal audio stays PCM16."""
from __future__ import annotations

import functools
import logging
import struct
import time
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterator

logger = logging.getLogger(__name__)
RATE = 48_000
FRAME_SAMPLES = 960
# Magic, stream, sequence, timestamp (microseconds), valid samples, leading discard.
HEADER = struct.Struct('!4sIIQII')
MAGIC = b'XAV2'
MAX_PACKET_SAMPLES = 5760  # Opus permits up to 120 ms per packet.


class AudioCodecError(RuntimeError):
    pass


@dataclass(frozen=True)
class AudioPacket:
    stream: int
    sequence: int
    timestamp: int
    samples: int
    discard: int
    payload: bytes

    def pack(self) -> bytes:
        return HEADER.pack(MAGIC, self.stream, self.sequence, self.timestamp, self.samples, self.discard) + self.payload

    @classmethod
    def unpack(cls, data: bytes) -> AudioPacket:
        if len(data) <= HEADER.size:
            raise ValueError('Missing audio packet header or payload')
        magic, stream, sequence, timestamp, samples, discard = HEADER.unpack_from(data)
        if magic != MAGIC or not stream or not 0 < samples <= MAX_PACKET_SAMPLES or discard + samples > MAX_PACKET_SAMPLES:
            raise ValueError('Invalid audio packet header')
        return cls(stream, sequence, timestamp, samples, discard, data[HEADER.size:])


@functools.lru_cache(maxsize=1)
def ensure_opus_ready() -> None:
    try:
        encoder = OpusEncoder()
        packets = list(encoder.push(b'\0\0' * FRAME_SAMPLES)) + list(encoder.finish())
        decoder = OpusDecoder()
        if sum(len(decoder.decode(packet)) for packet in packets) != FRAME_SAMPLES * 2:
            raise AudioCodecError("Opus self-test returned the wrong sample count")
    except Exception as exc:
        raise AudioCodecError('Voice codec initialization failed; check the PyAV installation') from exc


class OpusEncoder:
    """48 kHz mono s16 input; explicit pre-skip and tail trimming on raw packets."""
    def __init__(self, bitrate: int = 32_000):
        import av
        self.av = av
        self.codec = av.CodecContext.create('libopus', 'w')
        self.codec.sample_rate = RATE
        self.codec.layout = 'mono'
        self.codec.format = 's16'
        self.codec.bit_rate = bitrate
        self.codec.time_base = Fraction(1, RATE)
        self.codec.options = {'application': 'voip', 'frame_duration': '20', 'dtx': '0'}
        self.codec.open()
        self.buffer = bytearray()
        self.total = 0
        self.submitted = 0
        self.sequence = 0
        self.encode_ms = 0.0

    def _packets(self, packets) -> Iterator[AudioPacket]:
        for packet in packets:
            start = int(packet.pts)
            duration = int(packet.duration)
            valid_start = max(0, start)
            valid_end = min(self.total, start + duration)
            if valid_end > valid_start:
                yield AudioPacket(1, self.sequence, valid_start * 1_000_000 // RATE,
                                  valid_end - valid_start, max(0, -start), bytes(packet))
                self.sequence += 1

    def _encode(self, data: bytes):
        frame = self.av.AudioFrame(format='s16', layout='mono', samples=len(data) // 2)
        frame.sample_rate = RATE
        frame.pts = self.submitted
        frame.time_base = Fraction(1, RATE)
        frame.planes[0].update(data)
        self.submitted += frame.samples
        try:
            started = time.perf_counter()
            packets = self.codec.encode(frame)
            self.encode_ms += (time.perf_counter() - started) * 1000
            yield from self._packets(packets)
        except Exception as exc:
            raise AudioCodecError('Opus encoding failed') from exc

    def push(self, pcm: bytes) -> Iterator[AudioPacket]:
        if len(pcm) % 2:
            raise AudioCodecError('PCM is not sample-aligned')
        self.total += len(pcm) // 2
        self.buffer.extend(pcm)
        while len(self.buffer) >= FRAME_SAMPLES * 2:
            frame = bytes(self.buffer[:FRAME_SAMPLES * 2])
            del self.buffer[:FRAME_SAMPLES * 2]
            yield from self._encode(frame)

    def finish(self) -> Iterator[AudioPacket]:
        if self.buffer:
            # libopus supports a partial final frame and supplies encoder drain packets.
            yield from self._encode(bytes(self.buffer))
            self.buffer.clear()
        try:
            started = time.perf_counter()
            packets = self.codec.encode(None)
            self.encode_ms += (time.perf_counter() - started) * 1000
            yield from self._packets(packets)
            logger.debug('Opus encode audio_samples=%d encode_ms=%.3f', self.total, self.encode_ms)
        except Exception as exc:
            raise AudioCodecError('Opus encoder drain failed') from exc


class OpusDecoder:
    def __init__(self):
        import av
        self.av = av
        self.codec = av.CodecContext.create('opus', 'r')
        self.codec.sample_rate = RATE
        self.codec.layout = 'mono'
        self.codec.open()
        self.resampler = av.AudioResampler(format='s16', layout='mono', rate=RATE)

    def decode(self, packet: AudioPacket) -> bytes:
        try:
            started = time.perf_counter()
            frames = self.codec.decode(self.av.Packet(packet.payload))
            pcm = b''.join(bytes(out.planes[0])[:out.samples * 2]
                           for frame in frames for out in self.resampler.resample(frame))
            end = (packet.discard + packet.samples) * 2
            if end > len(pcm):
                raise ValueError('Decoded packet is shorter than declared samples')
            logger.debug('Opus decode audio_samples=%d decode_ms=%.3f', packet.samples, (time.perf_counter() - started) * 1000)
            return pcm[packet.discard * 2:end]
        except Exception as exc:
            raise AudioCodecError('Opus decoding failed') from exc
