import unittest

from xagent.interfaces.voice.echo_guard import SelfInterruptionGuard, looks_like_echo


class EchoDetectionTests(unittest.TestCase):
    def test_playback_leaking_into_the_mic_is_echo(self):
        self.assertTrue(
            looks_like_echo("the weather today is", "The weather today is mild and clear.")
        )

    def test_partial_transcript_of_our_own_words_is_echo(self):
        self.assertTrue(looks_like_echo("今天天气不错", "今天天气不错，适合出门。"))

    def test_a_real_interruption_is_not_echo(self):
        self.assertFalse(
            looks_like_echo("wait stop", "The weather today is mild and clear.")
        )

    def test_short_fragments_are_not_judged(self):
        self.assertFalse(looks_like_echo("ok", "okay then"))


class SelfInterruptionGuardTests(unittest.TestCase):
    def test_trips_after_repeated_echoes(self):
        guard = SelfInterruptionGuard()
        spoken = "The weather today is mild and clear."

        self.assertTrue(guard.classify("the weather today", spoken))
        self.assertFalse(guard.tripped)
        self.assertTrue(guard.classify("is mild and clear", spoken))
        self.assertTrue(guard.tripped)

    def test_real_speech_resets_the_count(self):
        guard = SelfInterruptionGuard()
        spoken = "The weather today is mild and clear."

        self.assertTrue(guard.classify("the weather today", spoken))
        self.assertFalse(guard.classify("hold on a second", spoken))
        self.assertTrue(guard.classify("the weather today", spoken))
        self.assertFalse(guard.tripped)


if __name__ == "__main__":
    unittest.main()
