import unittest

from xagent.interfaces.voice.barge_in import BargeInConfig, BargeInEvaluator


class VoiceBargeInTests(unittest.TestCase):
    def test_backchannel_does_not_interrupt(self):
        evaluator = BargeInEvaluator(BargeInConfig(min_speech_ms=100, min_words=2))
        evaluator._state.armed = True
        self.assertEqual(evaluator.evaluate_partial("嗯"), "backchannel")

    def test_short_partial_does_not_interrupt(self):
        evaluator = BargeInEvaluator(BargeInConfig(min_words=2))
        evaluator._state.armed = True
        self.assertEqual(evaluator.evaluate_partial("wait"), "none")

    def test_real_correction_interrupts(self):
        evaluator = BargeInEvaluator(BargeInConfig(min_words=2))
        evaluator._state.armed = True
        self.assertEqual(evaluator.evaluate_partial("no wait Tuesday"), "interrupt")


if __name__ == "__main__":
    unittest.main()
