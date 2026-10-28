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

    def __init__(self, utterance_text: str = "打开客厅灯") -> None:
        self.utterance_text = utterance_text
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
        for chunk in audio_chunks:
            if stop_event.is_set():
                return
            if not chunk or pause_event.is_set():
                continue
            self.received_chunks.append(chunk)
            yield VoiceUtterance(text=self.utterance_text, language="zh")
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

    async def test_paired_device_voice_session_inherits_room_location_and_streams_audio_and_control(self):
        host, _, _, url = await self.start_host([(ReplyType.SIMPLE_REPLY, "好的，已为您打开客厅灯。")])
        # Register the control socket first so the device's location is recorded in DeviceProfiles.
        async with websockets.connect(
            url + "/ws/devices/living-speaker",
            additional_headers={"Authorization": "Bearer speaker-secret"},
            **self.connection_options,
        ) as control_ws:
            manifest = device_test.manifest("living-speaker", location="客厅").model_dump()
            manifest["name"] = "客厅音箱"
            await control_ws.send(json.dumps(manifest))
            await self.wait_for(lambda: "living-speaker" in host.device_service.devices)

            fake_recognizer = _FakeWSRecognizer("打开客厅灯")
            fake_synth = _FakeWSSynthesizer()
            with patch(
                "xagent.interfaces.voice.factory.create_soniox_adapters",
                return_value=(fake_recognizer, fake_synth),
            ):
                async with websockets.connect(
                    url + "/ws/devices/living-speaker/voice",
                    additional_headers={"Authorization": "Bearer speaker-secret"},
                    **self.connection_options,
                ) as voice_ws:
                    await voice_ws.send(
                        json.dumps(
                            {
                                "type": "hello",
                                "input_sample_rate": 16000,
                                "input_channels": 1,
                                "output_sample_rate": 24000,
                                "output_channels": 1,
                                "profile": "headset",
                                "echo_managed": True,
                                "user_id": "alice",
                            }
                        )
                    )
                    ready = json.loads(await asyncio.wait_for(voice_ws.recv(), timeout=3))
                    self.assertEqual(ready["type"], "ready")
                    self.assertEqual(ready["device_id"], "living-speaker")
                    self.assertEqual(ready["room_name"], "客厅")

                    # Send 20ms of 16kHz PCM16 audio
                    await voice_ws.send(b"\x10\x00" * 320)

                    received_binary: list[bytes] = []
                    received_json: list[dict] = []
                    deadline = time.monotonic() + 5.0
                    while time.monotonic() < deadline:
                        frame = await asyncio.wait_for(voice_ws.recv(), timeout=3)
                        if isinstance(frame, bytes):
                            received_binary.append(frame)
                            await voice_ws.send(
                                json.dumps(
                                    {
                                        "type": "playback_progress",
                                        "played_bytes": sum(len(b) for b in received_binary),
                                    }
                                )
                            )
                        else:
                            payload = json.loads(frame)
                            received_json.append(payload)
                            if payload.get("type") == "playback_done":
                                break

                    self.assertTrue(fake_recognizer.received_chunks)
                    self.assertTrue(received_binary)
                    types = [item.get("type") for item in received_json]
                    self.assertIn("playback_start", types)
                    self.assertIn("playback_done", types)
                    user_transcripts = [
                        item
                        for item in received_json
                        if item.get("type") == "transcript" and item.get("role") == "user"
                    ]
                    self.assertEqual(user_transcripts[0]["text"], "打开客厅灯")
                    assistant_finals = [
                        item
                        for item in received_json
                        if item.get("type") == "transcript"
                        and item.get("role") == "assistant"
                        and item.get("final") is True
                    ]
                    self.assertEqual(assistant_finals[0]["text"], "好的，已为您打开客厅灯。")

                    # Verify that the Agent turn received room_name="客厅" and user_id="alice"
                    stored = await host.agent.message_storage.get_messages(10)
                    user_msg = next(msg for msg in stored if msg.sender_id == "alice")
                    self.assertEqual(user_msg.channel, "voice")
                    self.assertEqual(user_msg.room_name, "客厅")

                    # Verify ping/pong control message
                    await voice_ws.send(json.dumps({"type": "ping"}))
                    pong = json.loads(await asyncio.wait_for(voice_ws.recv(), timeout=2))
                    self.assertEqual(pong, {"type": "pong"})
                    await voice_ws.send(json.dumps({"type": "stop"}))

            await self.wait_for(lambda: not host.device_service.voice_sessions)

    async def test_pure_voice_device_can_pair_directly_on_voice_socket_and_reports_voice_online(self):
        host, _, _, url = await self.start_host([])
        code = host.device_service.create_pairing()["pairing_code"]
        fake_recognizer = _FakeWSRecognizer("早上好")
        fake_synth = _FakeWSSynthesizer()
        with patch(
            "xagent.interfaces.voice.factory.create_soniox_adapters",
            return_value=(fake_recognizer, fake_synth),
        ):
            async with websockets.connect(
                url + "/ws/devices/kitchen-mic/voice",
                additional_headers={"Authorization": "Bearer " + code},
                **self.connection_options,
            ) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "type": "hello",
                            "name": "厨房麦克风",
                            "location": "厨房",
                            "profile": "room",
                        }
                    )
                )
                ready = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
                self.assertEqual(ready["type"], "ready")
                self.assertEqual(ready["device_id"], "kitchen-mic")
                self.assertEqual(ready["room_name"], "厨房")
                issued = ready.get("credential", "")
                self.assertTrue(issued.startswith("dvc_"))

                # Pure-voice device appears in list_devices and admin status with voice_online=True
                listed = host.device_service.list_devices()["devices"]
                kitchen_entry = next(item for item in listed if item["device_id"] == "kitchen-mic")
                self.assertTrue(kitchen_entry["voice_online"])
                self.assertEqual(kitchen_entry["location"], "厨房")
                self.assertEqual(kitchen_entry["name"], "厨房麦克风")

                from xagent.core.runtime.client import RuntimeClient
                admin_rows = (await RuntimeClient(host.config_dir).request("GET", "/api/devices")).json()["devices"]
                admin_kitchen = next(item for item in admin_rows if item["device_id"] == "kitchen-mic")
                self.assertTrue(admin_kitchen["online"])
                self.assertTrue(admin_kitchen["voice_online"])

                await ws.send(json.dumps({"type": "stop"}))

            await self.wait_for(lambda: not host.device_service.voice_sessions)

            # One-time pairing code cannot be reused after pairing completed
            with self.assertRaises(Exception):
                async with websockets.connect(
                    url + "/ws/devices/kitchen-mic/voice",
                    additional_headers={"Authorization": "Bearer " + code},
                    **self.connection_options,
                ) as ws2:
                    await ws2.recv()

            # Issued dvc_ credential can reconnect without a control socket
            async with websockets.connect(
                url + "/ws/devices/kitchen-mic/voice",
                additional_headers={"Authorization": "Bearer " + issued},
                **self.connection_options,
            ) as ws3:
                await ws3.send(json.dumps({"type": "hello", "profile": "room"}))
                ready2 = json.loads(await asyncio.wait_for(ws3.recv(), timeout=3))
                self.assertEqual(ready2["type"], "ready")
                self.assertNotIn("credential", ready2)
                self.assertEqual(ready2["room_name"], "厨房")
                await ws3.send(json.dumps({"type": "stop"}))

    async def test_scheduled_task_and_subconscious_route_to_remote_device_voice_session(self):
        from xagent.core.runtime import ContactEntry, ScheduledTaskRecord, SubconsciousDelivery, load_contacts

        host, _, root, url = await self.start_host(
            [
                (ReplyType.SIMPLE_REPLY, "已为您设置提醒。"),
                (ReplyType.SIMPLE_REPLY, "主人，水已经烧开了。"),
            ]
        )
        fake_recognizer = _FakeWSRecognizer("十分钟后提醒我关火")
        fake_synth = _FakeWSSynthesizer()
        with patch(
            "xagent.interfaces.voice.factory.create_soniox_adapters",
            return_value=(fake_recognizer, fake_synth),
        ):
            async with websockets.connect(
                url + "/ws/devices/living-speaker/voice",
                additional_headers={"Authorization": "Bearer speaker-secret"},
                **self.connection_options,
            ) as voice_ws:
                await voice_ws.send(
                    json.dumps(
                        {
                            "type": "hello",
                            "name": "客厅音箱",
                            "location": "客厅",
                            "profile": "headset",
                            "echo_managed": True,
                            "user_id": "alice",
                        }
                    )
                )
                ready = json.loads(await asyncio.wait_for(voice_ws.recv(), timeout=3))
                self.assertEqual(ready["room_name"], "客厅")

                # Even though local voice channel is disconnected, remote voice session makes 'voice' deliverable
                self.assertEqual(host.channel_status["voice"]["state"], "disconnected")
                self.assertIn("voice", host._deliverable_channels())

                # Trigger one live voice turn so contacts.json is updated with device_id and room_name
                await voice_ws.send(b"\x10\x00" * 320)
                while True:
                    frame = await asyncio.wait_for(voice_ws.recv(), timeout=3)
                    if isinstance(frame, str) and json.loads(frame).get("type") == "playback_done":
                        break

                contacts = load_contacts(root / "contacts.json")
                voice_contact = next(c for c in contacts if c.channel == "voice" and c.user_id == "alice")
                self.assertEqual(voice_contact.target.get("device_id"), "living-speaker")
                self.assertEqual(voice_contact.target.get("room_name"), "客厅")

                # Execute and deliver a scheduled voice task targeted at this device
                from datetime import datetime, timezone
                task = ScheduledTaskRecord(
                    path=root / "tasks" / "task-kettle.json",
                    run_at=datetime(2026, 10, 16, 8, 0, tzinfo=timezone.utc),
                    kind="task",
                    payload={
                        "id": "task-kettle",
                        "title": "关火提醒",
                        "task": {"type": "message", "content": "请记得关火。"},
                        "delivery": {
                            "channel": "voice",
                            "user_id": "alice",
                            "target": {"user_id": "alice", "device_id": "living-speaker", "room_name": "客厅"},
                        },
                    },
                )
                self.assertTrue(host._can_handle_task(task))
                result = await host._execute_task(task)
                delivery_receipt = await host._deliver_task(task, result, "run-1")
                self.assertTrue(delivery_receipt["accepted"])
                self.assertGreater(delivery_receipt["audio_bytes"], 0)

                # Drain the scheduled task playback frames from the WebSocket
                scheduled_audio: list[bytes] = []
                while True:
                    frame = await asyncio.wait_for(voice_ws.recv(), timeout=3)
                    if isinstance(frame, bytes):
                        scheduled_audio.append(frame)
                    elif json.loads(frame).get("type") == "playback_done":
                        break
                self.assertTrue(scheduled_audio)

                # Deliver a subconscious voice message to the same device session
                sub_delivery = SubconsciousDelivery(
                    content="顺便提一下，外面好像要下雨了。",
                    recipient=voice_contact,
                    internal_content="Outside looks cloudy.",
                    created_at=datetime(2026, 10, 16, 8, 5, tzinfo=timezone.utc),
                )
                await host._deliver_subconscious(sub_delivery)
                sub_audio: list[bytes] = []
                while True:
                    frame = await asyncio.wait_for(voice_ws.recv(), timeout=3)
                    if isinstance(frame, bytes):
                        sub_audio.append(frame)
                    elif json.loads(frame).get("type") == "playback_done":
                        break
                self.assertTrue(sub_audio)

                await voice_ws.send(json.dumps({"type": "stop"}))

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

    async def test_revoke_device_closes_active_voice_session_and_resampling_works(self):
        host, _, _, url = await self.start_host([])
        fake_recognizer = _FakeWSRecognizer("测试重采样")
        fake_synth = _FakeWSSynthesizer()
        with patch(
            "xagent.interfaces.voice.factory.create_soniox_adapters",
            return_value=(fake_recognizer, fake_synth),
        ):
            async with websockets.connect(
                url + "/ws/devices/living-speaker/voice",
                additional_headers={"Authorization": "Bearer speaker-secret"},
                **self.connection_options,
            ) as voice_ws:
                await voice_ws.send(
                    json.dumps(
                        {
                            "type": "hello",
                            "input_sample_rate": 48000,
                            "input_channels": 2,
                            "output_sample_rate": 16000,
                            "output_channels": 1,
                            "profile": "room",
                            "echo_managed": False,
                        }
                    )
                )
                ready = json.loads(await asyncio.wait_for(voice_ws.recv(), timeout=3))
                self.assertEqual(ready["type"], "ready")
                # Without a prior profile location, room_name falls back to device_id
                self.assertEqual(ready["room_name"], "living-speaker")
                self.assertIn("living-speaker", host.device_service.voice_sessions)

                # Revoking the device closes the active voice session
                await host.device_service.revoke("living-speaker")
                await self.wait_for(lambda: not host.device_service.voice_sessions)
                with self.assertRaises(websockets.exceptions.ConnectionClosed):
                    while True:
                        await asyncio.wait_for(voice_ws.recv(), timeout=2)

    async def test_browser_query_token_pairs_phone_and_header_auth_still_connects(self):
        host, _, _, url = await self.start_host([], seed_credentials=False)
        with self.assertRaises(Exception):
            async with websockets.connect(
                url + "/ws/devices/phone-web/voice?token=not-a-credential",
                **self.connection_options,
            ) as rejected:
                await rejected.recv()

        code = host.device_service.create_pairing()["pairing_code"]
        hello = {
            "type": "hello",
            "name": "Phone mic",
            "input_sample_rate": 16000,
            "input_channels": 1,
            "output_sample_rate": 24000,
            "output_channels": 1,
            "profile": "headset",
            "echo_managed": True,
        }
        fake_recognizer = _FakeWSRecognizer("你好")
        fake_synth = _FakeWSSynthesizer()
        with patch(
            "xagent.interfaces.voice.factory.create_soniox_adapters",
            return_value=(fake_recognizer, fake_synth),
        ):
            async with websockets.connect(
                url + "/ws/devices/phone-web/voice?token=" + quote(code, safe=""),
                **self.connection_options,
            ) as phone:
                await phone.send(json.dumps(hello))
                ready = json.loads(await asyncio.wait_for(phone.recv(), timeout=3))
                self.assertEqual(ready["type"], "ready")
                self.assertEqual(ready["device_id"], "phone-web")
                self.assertEqual(ready["room_name"], "Phone mic")
                issued = ready["credential"]
                self.assertTrue(issued.startswith("dvc_"))
                listed = host.device_service.list_devices()["devices"]
                row = next(item for item in listed if item["device_id"] == "phone-web")
                self.assertTrue(row["voice_online"])
                self.assertEqual(row["name"], "Phone mic")
                await phone.send(json.dumps({"type": "stop"}))

            async with websockets.connect(
                url + "/ws/devices/phone-web/voice",
                additional_headers={"Authorization": "Bearer " + issued},
                **self.connection_options,
            ) as phone:
                await phone.send(json.dumps(hello))
                ready = json.loads(await asyncio.wait_for(phone.recv(), timeout=3))
                self.assertEqual(ready["type"], "ready")
                self.assertNotIn("credential", ready)
                await phone.send(json.dumps({"type": "stop"}))

            async with websockets.connect(
                url + "/ws/devices/phone-web/voice?token=not-a-credential",
                additional_headers={"Authorization": "Bearer " + issued},
                **self.connection_options,
            ) as phone:
                await phone.send(json.dumps(hello))
                ready = json.loads(await asyncio.wait_for(phone.recv(), timeout=3))
                self.assertEqual(ready["device_id"], "phone-web")
                await phone.send(json.dumps({"type": "stop"}))

            async with websockets.connect(
                url + "/ws/devices/phone-web/voice?token=" + quote(issued, safe=""),
                **self.connection_options,
            ) as phone:
                await phone.send(json.dumps(hello))
                ready = json.loads(await asyncio.wait_for(phone.recv(), timeout=3))
                self.assertEqual(ready["type"], "ready")
                self.assertNotIn("credential", ready)
                await phone.send(json.dumps({"type": "stop"}))

