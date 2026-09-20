import unittest

from xagent.interfaces.voice.floor import (
    ConversationFloor,
    FloorCommandKind,
    FloorEvent,
    FloorEventKind,
    FloorState,
)


class VoiceFloorTests(unittest.TestCase):
    def test_idle_endpoint_starts_thinking(self):
        floor = ConversationFloor()
        commands = floor.handle(FloorEvent(FloorEventKind.STT_ENDPOINT, text="hello"))
        self.assertEqual(floor.state, FloorState.THINKING)
        kinds = {item.kind for item in commands}
        self.assertIn(FloorCommandKind.START_THINK, kinds)

    def test_thinking_endpoint_steers(self):
        floor = ConversationFloor(state=FloorState.THINKING, in_flight_transcript="book Monday")
        commands = floor.handle(FloorEvent(FloorEventKind.STT_ENDPOINT, text="Tuesday"))
        self.assertEqual(commands[0].kind, FloorCommandKind.CANCEL_AGENT)
        self.assertEqual(commands[1].kind, FloorCommandKind.STEER_THINK)
        self.assertIn("Tuesday", commands[1].text)

    def test_speaking_barge_in_cancels_playback(self):
        floor = ConversationFloor(state=FloorState.SPEAKING)
        commands = floor.handle(FloorEvent(FloorEventKind.BARGE_IN_CONFIRMED, text="wait"))
        self.assertEqual(floor.state, FloorState.USER_SPEAKING)
        kinds = {item.kind for item in commands}
        self.assertIn(FloorCommandKind.CANCEL_TTS, kinds)
        self.assertIn(FloorCommandKind.CANCEL_AGENT, kinds)


if __name__ == "__main__":
    unittest.main()
