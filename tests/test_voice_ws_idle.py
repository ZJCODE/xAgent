"""Remote voice must feed Soniox silence while a push-to-talk device is quiet."""

from __future__ import annotations

import threading
import unittest

from xagent.interfaces.voice.ws import VoiceSessionHello, WebSocketMicrophone, WebSocketVoiceBridge


def _microphone() -> tuple[WebSocketMicrophone, threading.Event, threading.Event]:
    hello = VoiceSessionHello(
        input_sample_rate=16_000,
        input_channels=1,
        output_sample_rate=24_000,
        output_channels=1,
    )
    bridge = WebSocketVoiceBridge(
        websocket=None,
        loop=None,
        hello=hello,
        room_name="browser",
        send_lock=None,
        closed=threading.Event(),
    )
    microphone = WebSocketMicrophone(bridge)
    return microphone, threading.Event(), threading.Event()


class RemoteVoiceIdleSilenceTests(unittest.TestCase):
    def test_quiet_device_yields_silence_instead_of_an_empty_chunk(self) -> None:
        microphone, stop, pause = _microphone()
        frames = microphone.iter_chunks(pause_event=pause, stop_event=stop)
        frame = next(frames)
        stop.set()
        microphone.close()
        self.assertEqual(frame, b"\x00\x00" * 3200)
        self.assertTrue(set(frame) == {0})

    def test_paused_capture_does_not_invent_silence(self) -> None:
        microphone, stop, pause = _microphone()
        pause.set()
        frames = microphone.iter_chunks(pause_event=pause, stop_event=stop)
        frame = next(frames)
        stop.set()
        microphone.close()
        self.assertEqual(frame, b"")


if __name__ == "__main__":
    unittest.main()
