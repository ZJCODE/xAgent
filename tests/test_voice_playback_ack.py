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
