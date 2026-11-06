import unittest

from xagent.interfaces.voice.floor import (
    ConversationFloor,
    FloorCommandKind,
    FloorEvent,
    FloorEventKind,
    FloorState,
)


class VoiceFloorTests(unittest.TestCase):
    def test_idle_endpoint_mutes_capture_while_thinking(self):
        floor = ConversationFloor()
        commands = floor.handle(FloorEvent(FloorEventKind.STT_ENDPOINT, text="hello"))
        self.assertEqual(floor.state, FloorState.THINKING)
        kinds = {item.kind for item in commands}
        self.assertEqual(kinds, {FloorCommandKind.MIC_MUTE})

    def test_empty_endpoint_does_not_start_a_turn(self):
        floor = ConversationFloor()
        commands = floor.handle(FloorEvent(FloorEventKind.STT_ENDPOINT, text="  "))
        self.assertEqual(floor.state, FloorState.IDLE)
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0].kind, FloorCommandKind.EMIT_NOTICE)
        self.assertEqual(commands[0].notice_category, "not_understood")

    def test_duplex_turn_keeps_capture_open_and_releases_floor_after_playback(self):
        floor = ConversationFloor(allow_duplex_capture=True)
        commands = floor.handle(FloorEvent(FloorEventKind.STT_ENDPOINT, text="hello"))
        self.assertEqual([command.kind for command in commands], [FloorCommandKind.MIC_OPEN])
        floor.handle(FloorEvent(FloorEventKind.AGENT_FIRST_TEXT))
        self.assertEqual(floor.state, FloorState.SPEAKING)
        commands = floor.handle(FloorEvent(FloorEventKind.PLAYBACK_STOPPED))
        self.assertEqual(floor.state, FloorState.IDLE)
        self.assertEqual([command.kind for command in commands], [FloorCommandKind.MIC_OPEN])

    def test_thinking_endpoint_cancels_generation_before_runtime_restarts_it(self):
        floor = ConversationFloor(state=FloorState.THINKING)
        commands = floor.handle(FloorEvent(FloorEventKind.STT_ENDPOINT, text="Tuesday"))
        self.assertEqual(floor.state, FloorState.THINKING)
        self.assertEqual([command.kind for command in commands], [FloorCommandKind.CANCEL_AGENT])

    def test_speaking_barge_in_cancels_playback(self):
        floor = ConversationFloor(state=FloorState.SPEAKING)
        commands = floor.handle(FloorEvent(FloorEventKind.BARGE_IN_CONFIRMED, text="wait"))
        self.assertEqual(floor.state, FloorState.USER_SPEAKING)
        kinds = {item.kind for item in commands}
        self.assertIn(FloorCommandKind.CANCEL_TTS, kinds)
        self.assertIn(FloorCommandKind.CANCEL_AGENT, kinds)


if __name__ == "__main__":
    unittest.main()
