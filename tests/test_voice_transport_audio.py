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

from xagent.interfaces.voice.transport_audio import AudioPacket, OpusDecoder, OpusEncoder, opus_available
from xagent.interfaces.voice.ws import VoiceSessionHello, WebSocketVoiceBridge, WebSocketMicrophone, WebSocketPlayer


def bridge(hello):
    return WebSocketVoiceBridge(SimpleNamespace(), None, hello, 'test', asyncio.Lock(), threading.Event())


class AudioProtocolTests(unittest.TestCase):
    def test_packet_round_trip_and_rejects_invalid_headers(self):
        packet = AudioPacket(2, 3, 20000, 320, 0, b'\0\0' * 320)
        self.assertEqual(AudioPacket.unpack(packet.pack()), packet)
        for bad in (b'', b'XAV2', packet.pack()[:28], b'BAD!' + packet.pack()[4:], replace(packet, samples=0).pack(), replace(packet, samples=6000).pack()):
            with self.subTest(data=bad[:28]), self.assertRaises(ValueError):
                AudioPacket.unpack(bad)

    def test_legacy_needs_no_codecs_and_retains_requested_audio_rates(self):
        with patch('xagent.interfaces.voice.ws.opus_available', side_effect=AssertionError('Must not probe')):
            transport = bridge(VoiceSessionHello(input_sample_rate=48000, output_sample_rate=16000))
        self.assertEqual(transport.input_codec, 'pcm_s16le')
        self.assertEqual(transport.hello.input_sample_rate, 48000)
        self.assertEqual(transport.hello.output_sample_rate, 16000)

    def test_independent_codec_negotiation_and_missing_dependency_fallback(self):
        for available, offered_input, offered_output, expected in (
            (True, ['opus', 'pcm_s16le'], ['pcm_s16le'], ('opus', 'pcm_s16le', 48000, 24000)),
            (True, ['pcm_s16le'], ['opus', 'pcm_s16le'], ('pcm_s16le', 'opus', 16000, 48000)),
            (False, ['opus', 'pcm_s16le'], ['opus', 'pcm_s16le'], ('pcm_s16le', 'pcm_s16le', 16000, 24000)),
        ):
            with patch('xagent.interfaces.voice.ws.opus_available', return_value=available):
                transport = bridge(VoiceSessionHello(audio_protocol=2, playback_ack=True, input_codecs=offered_input, output_codecs=offered_output))
                self.assertEqual((transport.input_codec, transport.output_codec, transport.hello.input_sample_rate, transport.hello.output_sample_rate), expected)

    def test_pcm_stream_boundaries_ignore_old_packets_and_reject_gaps(self):
        transport = bridge(VoiceSessionHello(audio_protocol=2, playback_ack=True))
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
        mic = WebSocketMicrophone(bridge(VoiceSessionHello(audio_protocol=2, playback_ack=True)))
        mic.start_input(1)
        for n in range(25):
            mic.push_audio(AudioPacket(1, n, n * 20000, 320, 0, b'\0\0' * 320).pack())
        with self.assertRaisesRegex(ValueError, '500 ms'):
            mic.push_audio(AudioPacket(1, 25, 500000, 320, 0, b'\0\0' * 320).pack())
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
        self.assertTrue(opus_available())

    def test_output_resampling_flush_keeps_the_last_native_samples(self):
        transport = bridge(VoiceSessionHello(audio_protocol=2, playback_ack=True,
                                             input_codecs=['pcm_s16le'], output_codecs=['opus', 'pcm_s16le']))
        player = WebSocketPlayer(transport)
        packets = list(player._audio_packets(iter([b'\x10\0' * 24000]), threading.Event()))
        self.assertEqual(sum(packet.samples for packet in packets), 48000)
