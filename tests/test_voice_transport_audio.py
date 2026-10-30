"""Wire compatibility, real Opus priming/tail handling and bounded WS media queues."""
from __future__ import annotations

import asyncio
import math
import struct
import threading
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from xagent.interfaces.voice.transport_audio import AudioCodecError, AudioPacket, OpusDecoder, OpusEncoder, ensure_opus_ready
from xagent.interfaces.voice.ws import VoiceSessionHello, WebSocketVoiceBridge, WebSocketMicrophone, WebSocketPlayer


def bridge(hello):
    return WebSocketVoiceBridge(SimpleNamespace(), None, hello, 'test', asyncio.Lock(), threading.Event())


class AudioProtocolTests(unittest.TestCase):
    def test_codec_initialization_failure_is_explicit_even_for_pcm(self):
        ensure_opus_ready.cache_clear()
        try:
            with patch('xagent.interfaces.voice.transport_audio.OpusEncoder', side_effect=RuntimeError('Codec unavailable')):
                with self.assertRaisesRegex(AudioCodecError, 'PyAV'):
                    bridge(VoiceSessionHello(type='hello', audio_protocol=2, codec='pcm_s16le'))
        finally:
            ensure_opus_ready.cache_clear()

    def test_packet_round_trip_and_rejects_invalid_headers(self):
        packet = AudioPacket(2, 3, 20000, 320, 0, b'\0\0' * 320)
        self.assertEqual(AudioPacket.unpack(packet.pack()), packet)
        for bad in (b'', b'XAV2', packet.pack()[:28], b'BAD!' + packet.pack()[4:], replace(packet, samples=0).pack(), replace(packet, samples=6000).pack()):
            with self.subTest(data=bad[:28]), self.assertRaises(ValueError):
                AudioPacket.unpack(bad)

    def test_protocol_is_explicit_and_codec_profiles_are_fixed(self):
        for codec, rates in [('opus', (48000, 48000)), ('pcm_s16le', (16000, 24000))]:
            transport = bridge(VoiceSessionHello(type='hello', audio_protocol=2, codec=codec))
            self.assertEqual((transport.hello.input_sample_rate, transport.hello.output_sample_rate), rates)
        for values in ({}, {'audio_protocol': 1, 'codec': 'pcm_s16le'},
                       {'audio_protocol': 2, 'codec': 'opus', 'input_sample_rate': 16000},
                       {'audio_protocol': 2, 'codec': 'opus', 'input_codecs': ['pcm_s16le']}):
            with self.assertRaises(ValueError):
                VoiceSessionHello.model_validate({'type': 'hello', **values})

    def test_pcm_stream_boundaries_ignore_old_packets_and_reject_gaps(self):
        transport = bridge(VoiceSessionHello(type='hello', audio_protocol=2, codec='pcm_s16le'))
        mic = WebSocketMicrophone(transport)
        mic.start_input(1)
        mic.end_input(1)
        packet = AudioPacket(1, 0, 0, 320, 0, b'\1\0' * 320)
        mic.push_audio(packet.pack())  # Late audio from a muted stream is ignored.
        mic.start_input(2)
        mic.push_audio(replace(packet, stream=2).pack())
        generator = mic.iter_chunks(pause_event=threading.Event(), stop_event=threading.Event())
        self.assertEqual(next(generator), packet.payload)
        with self.assertRaises(ValueError):
            mic.push_audio(replace(packet, stream=2, sequence=4, timestamp=20000).pack())
        mic.close()
        generator.close()

    def test_audio_queue_is_bounded_by_duration(self):
        mic = WebSocketMicrophone(bridge(VoiceSessionHello(type='hello', audio_protocol=2, codec='pcm_s16le')))
        mic.start_input(1)
        for n in range(25):
            mic.push_audio(AudioPacket(1, n, n * 20000, 320, 0, b'\0\0' * 320).pack())
        with self.assertRaisesRegex(ValueError, '500 ms'):
            mic.push_audio(AudioPacket(1, 25, 500000, 320, 0, b'\0\0' * 320).pack())
        self.assertEqual(mic._queued_samples, 16000 // 2)
        self.assertEqual(mic._sequence, 25)
        mic.close()


class RealOpusTests(unittest.TestCase):
    def test_sample_exact_round_trip_for_tiny_and_partial_frames(self):
        for count in (1, 100, 648, 960, 961, 48000, 48001):
            with self.subTest(samples=count):
                encoder = OpusEncoder()
                data = b''.join(struct.pack('<h', int(8000 * math.sin(i * 2 * math.pi * 440 / 48000))) for i in range(count))
                packets = []
                for start in range(0, len(data), 254):
                    packets.extend(encoder.push(data[start:start + 254]))
                packets.extend(encoder.finish())
                decoder = OpusDecoder()
                restored = b''.join(decoder.decode(packet) for packet in packets)
                self.assertEqual(len(restored), len(data))
                self.assertEqual(sum(packet.samples for packet in packets), count)
                self.assertGreater(packets[0].discard, 0)
                for previous, current in zip(packets, packets[1:]):
                    self.assertEqual(current.sequence, previous.sequence + 1)
                if count >= 48000:
                    self.assertLess(sum(len(packet.pack()) for packet in packets), len(data) * .2)
                    original = struct.unpack('<' + 'h' * count, data)
                    actual = struct.unpack('<' + 'h' * count, restored)
                    # Ignore startup adaptation; check alignment and useful signal preservation.
                    dot = sum(a * b for a, b in zip(original[4800:], actual[4800:]))
                    norm = math.sqrt(sum(a*a for a in original[4800:]) * sum(b*b for b in actual[4800:]))
                    self.assertGreater(dot / norm, .8)
        ensure_opus_ready()

    def test_output_resampling_flush_keeps_the_last_native_samples(self):
        transport = bridge(VoiceSessionHello(type='hello', audio_protocol=2, codec='opus'))
        player = WebSocketPlayer(transport)
        packets = list(player._audio_packets(iter([b'\x10\0' * 24000]), threading.Event()))
        self.assertEqual(sum(packet.samples for packet in packets), 48000)
