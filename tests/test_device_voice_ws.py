"""Tests for paired device WebSocket voice sessions (/ws/devices/{device_id}/voice)."""
from __future__ import annotations

import asyncio
import inspect
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import websockets
import yaml

import tests.test_agent_chat_flow as chat_test
import tests.test_device_execution as device_test
import tests.test_runtime_host as runtime_test
from xagent.core.config import ReplyType
from xagent.integrations.devices.credentials import DeviceCredentials
from xagent.interfaces.voice.types import VoiceUtterance


class _FakeWSRecognizer:
    """Recognizer that consumes pushed WebSocket PCM chunks and yields a fixed utterance."""

    def __init__(self, utterance_text: str = "打开客厅灯", *, continuous: bool = False) -> None:
        self.utterance_text = utterance_text
        self.continuous = continuous
        self.received_chunks: list[bytes] = []
        self.resources_closed = True

    def set_extra_context_terms(self, terms: list[str]) -> None:
        self.extra_terms = list(terms)

    def iter_utterances(
        self,
        audio_chunks,
        *,
        pause_event: threading.Event,
        stop_event: threading.Event,
    ):
        spoken = False
        for chunk in audio_chunks:
            if stop_event.is_set():
                return
            if not chunk:
                spoken = False
                continue
            if pause_event.is_set():
                continue
            self.received_chunks.append(chunk)
            if spoken:
                continue
            spoken = True
            yield VoiceUtterance(text=self.utterance_text, language="zh")
            if not self.continuous:
                return

    def close(self) -> None:
        self.resources_closed = True


class _FakeWSSynthesizer:
    """Synthesizer that yields 24kHz mono PCM bytes for streamed reply text."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.cancelled = False

    def synthesize_chunks(
        self,
        text_chunks,
        *,
        language: str,
        stop_event: threading.Event,
        **kwargs,
    ):
        spoken_ledger = kwargs.get("spoken_ledger")
        chunks = list(text_chunks)
        self.calls.append({"language": language, "chunks": chunks})
        full_text = "".join(chunks)
        audio = b"\x01\x00" * 480  # 20ms at 24kHz mono PCM16 (960 bytes)
        if spoken_ledger is not None and full_text:
            spoken_ledger.ingest_timestamps(
                characters=list(full_text),
                character_end_times_seconds=[0.01 * (i + 1) for i in range(len(full_text))],
            )
        if not stop_event.is_set():
            yield audio

    def cancel(self) -> None:
        self.cancelled = True

    def close(self) -> None:
        pass


class DeviceVoiceWebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hosts = []
        self.connection_options = (
            {"proxy": None} if "proxy" in inspect.signature(websockets.connect).parameters else {}
        )
        cooldown = patch("xagent.interfaces.voice.runtime._PLAYBACK_MICROPHONE_COOLDOWN_SECONDS", 0.0)
        cooldown.start()
        self.addCleanup(cooldown.stop)

    async def asyncTearDown(self):
        for host, task in reversed(self.hosts):
            await host.stop()
            await asyncio.wait_for(task, timeout=20)
            self.assertTrue(host.device_service.resources_closed)

    async def wait_for(self, condition, timeout: float = 3.0):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail("Runtime condition did not become ready")
            await asyncio.sleep(0.01)

    async def start_host(self, responses, *, seed_credentials: bool = True, voice_api_key: str = "test-soniox-key"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        runtime_test.write_config(root)
        config = yaml.safe_load((root / "config.yaml").read_text())
        config["channels"]["api"] = {"enabled": True, "host": "127.0.0.1", "port": port}
        config["channels"]["voice"] = {
            "api_key": voice_api_key,
            "voice": "Daniel",
            "interruptions": False,
        }
        (root / "config.yaml").write_text(yaml.safe_dump(config))
        if seed_credentials:
            DeviceCredentials(root / ".devices.json").add("living-speaker", "speaker-secret")
        host, task = await runtime_test.RuntimeTransportTests._start_host(
            self, root, chat_test.CapturingModelClient(responses)
        )
        self.hosts.append((host, task))
        await self.wait_for(lambda: host.channel_status["api"]["state"] == "connected")
        return host, task, root, f"ws://127.0.0.1:{port}"

    async def test_sensor_only_device_registers_with_empty_capabilities(self):
        host, _, _, url = await self.start_host([])
        async with websockets.connect(
            url + "/ws/devices/living-speaker",
            additional_headers={"Authorization": "Bearer speaker-secret"},
            **self.connection_options,
        ) as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "register",
                        "device_id": "living-speaker",
                        "name": "玄关人体传感器",
                        "location": "玄关",
                        "metadata": {"device_type": "sensor"},
                        "capabilities": [],
                    }
                )
            )
            await self.wait_for(lambda: "living-speaker" in host.device_service.devices)
            listed = host.device_service.list_devices(device_id="living-speaker")["devices"]
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["capabilities"], [])
            self.assertFalse(listed[0]["voice_online"])


    async def send_audio(self, ws, codec='pcm_s16le', stream=1):
        from xagent.interfaces.voice.transport_audio import AudioPacket, OpusEncoder
        await ws.send(json.dumps({'type': 'input_start', 'stream_id': stream}))
        if codec == 'opus':
            encoder = OpusEncoder(24000)
            packets = list(encoder.push(b'\x10\0' * 960)) + list(encoder.finish())
        else:
            packets = [AudioPacket(stream, 0, 0, 320, 0, b'\x10\0' * 320)]
        from dataclasses import replace
        for packet in packets:
            await ws.send(replace(packet, stream=stream).pack())
        await ws.send(json.dumps({'type': 'input_end', 'stream_id': stream}))

    async def playback(self, ws, rate=24000):
        from xagent.interfaces.voice.transport_audio import AudioPacket, OpusDecoder
        samples = 0
        decoder = OpusDecoder() if rate == 48000 else None
        while True:
            message = await asyncio.wait_for(ws.recv(), timeout=4)
            if isinstance(message, bytes):
                packet = AudioPacket.unpack(message)
                pcm = decoder.decode(packet) if decoder else packet.payload
                self.assertEqual(len(pcm), packet.samples * 2)
                samples += packet.samples
            else:
                frame = json.loads(message)
                if frame['type'] == 'playback_done':
                    await ws.send(json.dumps({'type': 'playback_progress', 'playback_id': frame['playback_id'], 'played_samples': samples, 'sample_rate': rate}))
                    await ws.send(json.dumps({'type': 'playback_finished', 'playback_id': frame['playback_id']}))
                    return samples

    async def test_device_pairing_profile_and_audio_use_one_protocol(self):
        host, _, root, url = await self.start_host([(ReplyType.SIMPLE_REPLY, '好的。')], seed_credentials=False)
        code = host.device_service.create_pairing()['pairing_code']
        with patch('xagent.interfaces.voice.factory.create_soniox_adapters', return_value=(_FakeWSRecognizer(continuous=True), _FakeWSSynthesizer())):
            async with websockets.connect(url + '/ws/devices/desk/voice?token=' + quote(code, safe=''), **self.connection_options) as ws:
                await ws.send(json.dumps({'type': 'hello', 'audio_protocol': 2, 'codec': 'pcm_s16le', 'profile': 'headset', 'name': 'Desk', 'location': 'Office', 'user_id': 'alice'}))
                ready = json.loads(await ws.recv())
                self.assertEqual(ready['codec'], 'pcm_s16le')
                self.assertTrue(ready['credential'].startswith('dvc_'))
                self.assertEqual(ready['room_name'], 'Office')
                await self.send_audio(ws)
                self.assertGreater(await self.playback(ws), 0)
                await self.wait_for(lambda: not host.device_service.voice_sessions['desk'].runtime.pause_event.is_set())
                from xagent.core.runtime import load_contacts
                contact = next(c for c in load_contacts(root / 'contacts.json') if c.channel == 'voice' and c.user_id == 'alice')
                self.assertEqual(contact.target['device_id'], 'desk')
                self.assertEqual(contact.target['room_name'], 'Office')
                self.assertIn('voice', host._deliverable_channels())
                await ws.send(json.dumps({'type': 'ping'}))
                self.assertEqual(json.loads(await ws.recv())['type'], 'pong')
                await ws.send(json.dumps({'type': 'stop'}))
            await self.wait_for(lambda: not host.device_service.voice_sessions)
            async with websockets.connect(url + '/ws/devices/desk/voice', additional_headers={'Authorization': 'Bearer ' + ready['credential']}, **self.connection_options) as ws:
                await ws.send(json.dumps({'type': 'hello', 'audio_protocol': 2, 'codec': 'pcm_s16le'}))
                self.assertNotIn('credential', json.loads(await ws.recv()))
                await host.device_service.revoke('desk')
                await self.wait_for(lambda: not host.device_service.voice_sessions)

    async def test_browser_three_turns_in_both_profiles_and_single_use_tickets(self):
        from xagent.core.runtime.client import RuntimeClient
        for codec in ('pcm_s16le', 'opus'):
            with self.subTest(codec=codec):
                host, _, root, url = await self.start_host([(ReplyType.SIMPLE_REPLY, '你好。') for _ in range(6)], seed_credentials=False)
                ticket = (await RuntimeClient(root).request('POST', '/api/devices/voice-session')).json()
                target = url + '/ws/voice/' + ticket['session_id'] + '?token=' + quote(ticket['token'], safe='')
                recognizer = _FakeWSRecognizer(continuous=True)
                with patch('xagent.interfaces.voice.factory.create_soniox_adapters', return_value=(recognizer, _FakeWSSynthesizer())):
                    async with websockets.connect(target, **self.connection_options) as ws:
                        await ws.send(json.dumps({'type': 'hello', 'audio_protocol': 2, 'codec': codec, 'profile': 'headset'}))
                        ready = json.loads(await ws.recv())
                        self.assertEqual(ready['codec'], codec)
                        runtime = host.device_service.voice_sessions[ticket['session_id']].runtime
                        runtime._attention._window_open_until = 0
                        self.assertTrue(runtime.options.direct_conversation)
                        self.assertEqual(host.device_service.list_devices()['devices'], [])
                        for stream in range(1, 4):
                            await self.send_audio(ws, codec, stream)
                            self.assertGreater(await self.playback(ws, ready['output_sample_rate']), 0)
                            await self.wait_for(lambda: not runtime.pause_event.is_set())
                            await asyncio.sleep(.21)  # No speech while the fake recognizer resets its endpoint.
                        await ws.send(json.dumps({'type': 'stop'}))
                    await self.wait_for(lambda: not host.device_service.voice_sessions)
                with self.assertRaises(Exception):
                    async with websockets.connect(target, **self.connection_options):
                        pass

    async def test_old_protocol_and_byte_progress_are_rejected_explicitly(self):
        host, _, _, url = await self.start_host([])
        with patch('xagent.interfaces.voice.factory.create_soniox_adapters', return_value=(_FakeWSRecognizer(continuous=True), _FakeWSSynthesizer())):
            for hello in ({'type': 'hello'}, {'type': 'hello', 'audio_protocol': 1}):
                async with websockets.connect(url + '/ws/devices/living-speaker/voice', additional_headers={'Authorization': 'Bearer speaker-secret'}, **self.connection_options) as ws:
                    await ws.send(json.dumps(hello))
                    with self.assertRaises(websockets.exceptions.ConnectionClosed) as caught:
                        await ws.recv()
                    self.assertIn('protocol 2', str(caught.exception))
            async with websockets.connect(url + '/ws/devices/living-speaker/voice', additional_headers={'Authorization': 'Bearer speaker-secret'}, **self.connection_options) as ws:
                await ws.send(json.dumps({'type': 'hello', 'audio_protocol': 2, 'codec': 'pcm_s16le'})); await ws.recv()
                await ws.send(json.dumps({'type': 'playback_progress', 'playback_id': 'old', 'played_bytes': 100}))
                with self.assertRaises(websockets.exceptions.ConnectionClosed): await ws.recv()

    async def test_remote_scheduled_output_waits_for_playback_acknowledgement(self):
        from datetime import datetime, timezone
        from xagent.core.runtime import ScheduledTaskRecord
        host, _, root, url = await self.start_host([])
        with patch('xagent.interfaces.voice.factory.create_soniox_adapters', return_value=(_FakeWSRecognizer(continuous=True), _FakeWSSynthesizer())):
            async with websockets.connect(url + '/ws/devices/living-speaker/voice', additional_headers={'Authorization': 'Bearer speaker-secret'}, **self.connection_options) as ws:
                await ws.send(json.dumps({'type': 'hello', 'audio_protocol': 2, 'codec': 'pcm_s16le'})); await ws.recv()
                task = ScheduledTaskRecord(path=root/'tasks'/'reminder.json', run_at=datetime.now(timezone.utc), kind='task', payload={'id': 'reminder', 'title': 'Reminder', 'task': {'type': 'message', 'content': '喝水。'}, 'delivery': {'channel': 'voice', 'user_id': 'alice', 'target': {'device_id': 'living-speaker', 'user_id': 'alice'}}})
                result = await host._execute_task(task)
                delivery = asyncio.create_task(host._deliver_task(task, result, 'run-1'))
                self.assertGreater(await self.playback(ws), 0)
                self.assertTrue((await delivery)['accepted'])
                await ws.send(json.dumps({'type': 'stop'}))


    async def test_ready_waits_for_slow_recognition_consumer(self):
        class SlowRecognizer(_FakeWSRecognizer):
            def iter_utterances(self, audio_chunks, *, pause_event, stop_event):
                stop_event.wait(.75)  # Model provider connection/context refresh.
                yield from super().iter_utterances(audio_chunks, pause_event=pause_event, stop_event=stop_event)
        host, _, _, url = await self.start_host([(ReplyType.SIMPLE_REPLY, '好的。')])
        with patch('xagent.interfaces.voice.factory.create_soniox_adapters', return_value=(SlowRecognizer(continuous=True), _FakeWSSynthesizer())):
            async with websockets.connect(url + '/ws/devices/living-speaker/voice', additional_headers={'Authorization': 'Bearer speaker-secret'}, **self.connection_options) as ws:
                started = time.monotonic()
                await ws.send(json.dumps({'type': 'hello', 'audio_protocol': 2, 'codec': 'pcm_s16le', 'profile': 'headset'}))
                ready = json.loads(await asyncio.wait_for(ws.recv(), 3))
                self.assertEqual(ready['type'], 'ready')
                self.assertGreater(time.monotonic() - started, .7)
                await self.send_audio(ws)
                self.assertGreater(await self.playback(ws), 0)
                await ws.send(json.dumps({'type': 'stop'}))

    async def test_stalled_consumer_overload_is_retryable(self):
        from xagent.interfaces.voice.transport_audio import AudioPacket
        consumed = threading.Event()
        class StalledRecognizer(_FakeWSRecognizer):
            def iter_utterances(self, audio_chunks, *, pause_event, stop_event):
                for chunk in audio_chunks:
                    if chunk:
                        consumed.set()
                        stop_event.wait(3)
                        return
                yield  # Keep the recognizer interface a generator.
        host, _, _, url = await self.start_host([])
        with patch('xagent.interfaces.voice.factory.create_soniox_adapters', return_value=(StalledRecognizer(), _FakeWSSynthesizer())):
            async with websockets.connect(url + '/ws/devices/living-speaker/voice', additional_headers={'Authorization': 'Bearer speaker-secret'}, **self.connection_options) as ws:
                await ws.send(json.dumps({'type': 'hello', 'audio_protocol': 2, 'codec': 'pcm_s16le'}))
                await ws.recv()
                await ws.send(json.dumps({'type': 'input_start', 'stream_id': 1}))
                await ws.send(AudioPacket(1, 0, 0, 320, 0, b'\0\0' * 320).pack())
                self.assertTrue(await asyncio.to_thread(consumed.wait, 1))
                for n in range(1, 27):
                    await ws.send(AudioPacket(1, n, n * 20000, 320, 0, b'\0\0' * 320).pack())
                with self.assertRaises(websockets.exceptions.ConnectionClosed) as caught:
                    await ws.recv()
                self.assertEqual(caught.exception.rcvd.code, 1013)
