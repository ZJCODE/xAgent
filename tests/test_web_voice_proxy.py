"""Browser voice uses the existing device voice socket via the web client relay."""

from __future__ import annotations

import logging
import socket
import threading
import time
import unittest

import uvicorn
from fastapi import FastAPI, WebSocket
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from xagent.interfaces.web.proxy import register_api_proxy


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until_listening(port: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("voice upstream did not start")


class WebVoiceProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.seen: dict[str, list] = {"auth": [], "urls": [], "messages": []}
        upstream = FastAPI()
        seen = self.seen

        @upstream.websocket("/ws/devices/{device_id}/voice")
        async def voice(websocket: WebSocket, device_id: str):
            seen["auth"].append(websocket.headers.get("authorization", ""))
            seen["urls"].append(str(websocket.url))
            token = websocket.headers.get("authorization", "").removeprefix("Bearer ").strip()
            if token == "reject-me":
                await websocket.close(code=1008)
                return
            await websocket.accept()
            hello = await websocket.receive_json()
            seen["messages"].append(hello)
            await websocket.send_json({
                "type": "ready",
                "device_id": device_id,
                "room_name": "browser",
                "credential": "dvc_issued",
                "input_sample_rate": hello.get("input_sample_rate", 16000),
                "output_sample_rate": hello.get("output_sample_rate", 24000),
            })
            audio = await websocket.receive_bytes()
            seen["messages"].append(audio)
            await websocket.send_bytes(b"\x01\x00\x02\x00")
            progress = await websocket.receive_json()
            seen["messages"].append(progress)
            await websocket.send_json({"type": "playback_done"})

        self.port = _free_port()
        config = uvicorn.Config(upstream, host="127.0.0.1", port=self.port, log_level="warning", lifespan="off")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        _wait_until_listening(self.port)
        self.proxy = FastAPI()
        register_api_proxy(
            self.proxy,
            resolve_api_url=lambda: f"http://127.0.0.1:{self.port}",
            logger=logging.getLogger("test.web.voice"),
        )

    def tearDown(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)

    def test_relay_injects_authorization_and_forwards_voice_frames(self) -> None:
        with TestClient(self.proxy) as client:
            with client.websocket_connect("/ws/devices/webvoice1/voice?token=should-not-leak") as ws:
                ws.send_json({"type": "auth", "token": "pair_once"})
                ws.send_json({
                    "type": "hello",
                    "name": "Web browser",
                    "location": "browser",
                    "input_sample_rate": 16000,
                    "input_channels": 1,
                    "output_sample_rate": 24000,
                    "output_channels": 1,
                    "profile": "headset",
                    "echo_managed": True,
                    "interruptions": True,
                    "user_id": "web_user",
                })
                ready = ws.receive_json()
                self.assertEqual(ready["type"], "ready")
                self.assertEqual(ready["credential"], "dvc_issued")
                self.assertEqual(ready["device_id"], "webvoice1")
                ws.send_bytes(b"\x10\x00" * 8)
                audio = ws.receive_bytes()
                self.assertEqual(audio, b"\x01\x00\x02\x00")
                ws.send_json({"type": "playback_progress", "played_bytes": 4})
                done = ws.receive_json()
                self.assertEqual(done, {"type": "playback_done"})

        self.assertEqual(self.seen["auth"], ["Bearer pair_once"])
        self.assertNotIn("should-not-leak", self.seen["urls"][0])
        self.assertNotIn("pair_once", self.seen["urls"][0])
        hello = self.seen["messages"][0]
        self.assertEqual(hello["type"], "hello")
        self.assertEqual(hello["profile"], "headset")
        self.assertIs(hello["echo_managed"], True)
        self.assertIs(hello["interruptions"], True)
        self.assertNotIn("auth", [item.get("type") for item in self.seen["messages"] if isinstance(item, dict)])
        self.assertEqual(self.seen["messages"][1], b"\x10\x00" * 8)
        self.assertEqual(self.seen["messages"][2], {"type": "playback_progress", "played_bytes": 4})

    def test_missing_auth_frame_never_reaches_the_voice_socket(self) -> None:
        with TestClient(self.proxy) as client:
            with client.websocket_connect("/ws/devices/webvoice1/voice") as ws:
                ws.send_json({"type": "hello", "profile": "headset"})
                with self.assertRaises(WebSocketDisconnect) as raised:
                    ws.receive_text()
        self.assertEqual(raised.exception.code, 1008)
        self.assertEqual(self.seen["auth"], [])

    def test_rejected_device_credential_closes_as_unauthorized(self) -> None:
        with TestClient(self.proxy) as client:
            with client.websocket_connect("/ws/devices/webvoice1/voice") as ws:
                ws.send_json({"type": "auth", "token": "reject-me"})
                with self.assertRaises(WebSocketDisconnect) as raised:
                    ws.receive_text()
        self.assertEqual(raised.exception.code, 1008)
        self.assertEqual(self.seen["auth"], ["Bearer reject-me"])


if __name__ == "__main__":
    unittest.main()
