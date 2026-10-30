"""Playback completion follows the remote audio clock, not transport delivery."""
from __future__ import annotations

import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from xagent.interfaces.voice.ws import WebSocketPlayer, VoiceSessionHello


class Bridge:
    def __init__(self):
        self.hello = VoiceSessionHello(type='hello', audio_protocol=2, codec='pcm_s16le')
        self.closed = threading.Event()
        self.done = threading.Event()
        self.frames = []
        self.audio = []
        self.close_scheduled = False

    def send_json(self, frame):
        self.frames.append(frame)
        if frame['type'] == 'playback_done':
            self.done.set()

    def send_bytes(self, data):
        self.audio.append(data)

    def _schedule_send(self, _work):
        self.close_scheduled = True


class PlaybackAcknowledgementTests(unittest.IsolatedAsyncioTestCase):
    async def begin(self, bridge, player=None):
        player = player or WebSocketPlayer(bridge)
        stop = threading.Event()
        task = asyncio.create_task(asyncio.to_thread(player.play_chunks, iter([b'\x00\x00' * 240]), stop_event=stop))
        self.addAsyncCleanup(self.cleanup_playback, bridge, task)
        self.assertTrue(await asyncio.to_thread(bridge.done.wait, 1))
        return player, stop, task

    async def cleanup_playback(self, bridge, task):
        bridge.closed.set()
        await asyncio.gather(task, return_exceptions=True)

    async def test_does_not_finish_until_matching_browser_confirmation(self):
        bridge = Bridge()
        player, _, task = await self.begin(bridge)
        playback_id = bridge.frames[0]['playback_id']
        self.assertEqual(bridge.frames[-1]['playback_id'], playback_id)
        self.assertFalse(task.done())
        player.note_playback_finished('previous-playback')
        await asyncio.sleep(0.06)
        self.assertFalse(task.done())
        self.assertFalse(player.accepts_playback('previous-playback'))
        self.assertFalse(player.accepts_playback(None))
        self.assertTrue(player.accepts_playback(playback_id))
        player.note_playback_finished(playback_id)
        await asyncio.wait_for(task, 1)
        self.assertFalse(player.accepts_playback(playback_id))

    async def test_interrupt_clears_buffer_without_waiting_for_confirmation(self):
        bridge = Bridge()
        _, stop, task = await self.begin(bridge)
        stop.set()
        await asyncio.wait_for(task, 1)
        self.assertEqual(bridge.frames[-1]['type'], 'clear_playback')
        self.assertEqual(bridge.frames[-1]['playback_id'], bridge.frames[0]['playback_id'])

    async def test_disconnect_releases_waiter(self):
        bridge = Bridge()
        _, _, task = await self.begin(bridge)
        bridge.closed.set()
        await asyncio.wait_for(task, 1)

    async def test_previous_confirmation_cannot_finish_next_playback(self):
        bridge = Bridge()
        player, _, first = await self.begin(bridge)
        first_id = bridge.frames[0]['playback_id']
        player.note_playback_finished(first_id)
        await first
        bridge.done.clear()
        _, _, second = await self.begin(bridge, player)
        second_id = bridge.frames[-1]['playback_id']
        self.assertNotEqual(second_id, first_id)
        player.note_playback_finished(first_id)
        await asyncio.sleep(0.06)
        self.assertFalse(second.done())
        player.note_playback_finished(second_id)
        await asyncio.wait_for(second, 1)

    async def test_timeout_closes_session(self):
        bridge = Bridge()
        with patch('xagent.interfaces.voice.ws.time') as clock:
            clock.monotonic.side_effect = [0, 0, 0, 100]
            player, _, task = await self.begin(bridge)
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(task, 1)
        self.assertTrue(bridge.closed.is_set())
        self.assertTrue(bridge.close_scheduled)
        self.assertEqual(bridge.frames[-1]['code'], 'playback_timeout')


class PlaybackFlowControlTests(unittest.IsolatedAsyncioTestCase):
    async def start_playback(self, *, seconds=2, codec='pcm_s16le'):
        from xagent.interfaces.voice.transport_audio import AudioPacket
        class Receiver(Bridge):
            def __init__(self):
                super().__init__()
                self.hello = VoiceSessionHello(type='hello', audio_protocol=2, codec=codec)
                self.received = 0
                self.max_pending = 0
                self.rendered = 0
                self.available = threading.Condition()
            def send_bytes(self, data):
                with self.available:
                    self.received += AudioPacket.unpack(data).samples
                    self.max_pending = max(self.max_pending, self.received - self.rendered)
                    self.available.notify_all()
            def consume(self, player, samples):
                with self.available:
                    self.rendered = samples
                player.note_playback_progress(self.frames[0]['playback_id'], samples)
            def wait_samples(self, samples):
                with self.available:
                    return self.available.wait_for(lambda: self.received >= samples, timeout=1)
        bridge = Receiver()
        player = WebSocketPlayer(bridge, playback_flow_control=True)
        stop = threading.Event()
        task = asyncio.create_task(asyncio.to_thread(player.play_chunks,
            iter([b'\0\0' * int(24000 * seconds)]), stop_event=stop))
        async def cleanup():
            bridge.closed.set()
            await asyncio.gather(task, return_exceptions=True)
        self.addAsyncCleanup(cleanup)
        # Opus priming can leave <20 ms unused in the first window.
        self.assertTrue(await asyncio.to_thread(bridge.wait_samples, int(player.stream_sample_rate * .28)))
        return bridge, player, stop, task

    async def test_renderer_stalls_do_not_earn_wall_clock_credit_or_overflow(self):
        from xagent.interfaces.voice.ws import PLAYBACK_WINDOW_SECONDS
        for codec in ('pcm_s16le', 'opus'):
            with self.subTest(codec=codec):
                bridge, player, _, task = await self.start_playback(seconds=3, codec=codec)
                window = int(player.stream_sample_rate * PLAYBACK_WINDOW_SECONDS)
                await asyncio.sleep(.35)  # Old sender would now enqueue >500 ms.
                self.assertLessEqual(bridge.received, window)
                self.assertFalse(task.done())
                # Repeated refill cycles, delayed progress and a slow media clock.
                target = player.stream_sample_rate * 3
                while bridge.received < target:
                    consumed = bridge.received
                    bridge.consume(player, consumed)
                    self.assertTrue(await asyncio.to_thread(bridge.wait_samples, min(target, consumed + int(player.stream_sample_rate * .28))))
                    self.assertLessEqual(bridge.max_pending, window)
                self.assertTrue(await asyncio.to_thread(bridge.done.wait, 1))
                bridge.consume(player, target)
                player.note_playback_finished(bridge.frames[0]['playback_id'])
                await asyncio.wait_for(task, 1)
                self.assertEqual(bridge.received, target)
                self.assertLessEqual(bridge.max_pending, window)

    async def test_blocked_sender_can_be_interrupted_or_disconnected(self):
        for action in ('interrupt', 'disconnect'):
            with self.subTest(action=action):
                bridge, _, stop, task = await self.start_playback()
                (stop if action == 'interrupt' else bridge.closed).set()
                await asyncio.wait_for(task, 1)
                self.assertEqual(any(f['type'] == 'clear_playback' for f in bridge.frames), action == 'interrupt')

    async def test_stale_or_impossible_progress_cannot_grant_send_credit(self):
        bridge, player, stop, task = await self.start_playback()
        player.note_playback_progress('previous-playback', 999999)
        self.assertEqual(player._rendered_samples, 0)
        with self.assertRaises(ValueError):
            player.note_playback_progress(bridge.frames[0]['playback_id'], player.sent_samples + 1)
        bridge.consume(player, 480)
        player.note_playback_progress(bridge.frames[0]['playback_id'], 0)
        self.assertEqual(player._rendered_samples, 480)
        stop.set()
        await asyncio.wait_for(task, 1)

    async def test_no_render_progress_times_out_instead_of_buffering_unbounded_audio(self):
        with patch('xagent.interfaces.voice.ws.PLAYBACK_PROGRESS_TIMEOUT_SECONDS', .1):
            bridge, _, _, task = await self.start_playback()
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(task, 1)
        self.assertTrue(bridge.closed.is_set())
        self.assertTrue(bridge.close_scheduled)
        self.assertEqual(bridge.frames[-1]['code'], 'playback_timeout')
