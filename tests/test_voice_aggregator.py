import threading
import time
import unittest

from xagent.interfaces.voice.aggregator import iter_aggregated_utterances
from xagent.interfaces.voice.types import VoiceUtterance


class VoiceAggregatorTests(unittest.TestCase):
    def test_merges_segments_within_grace_window(self):
        stop = threading.Event()

        def source():
            yield VoiceUtterance("我想问一下", "zh")
            time.sleep(0.05)
            yield VoiceUtterance("然后明天可以吗", "zh")

        merged = list(iter_aggregated_utterances(source(), stop_event=stop))
        self.assertEqual(len(merged), 1)
        self.assertIn("我想问一下", merged[0].text)
        self.assertIn("然后明天可以吗", merged[0].text)

    def test_dispatches_separate_turns_after_grace_expires(self):
        stop = threading.Event()

        def source():
            yield VoiceUtterance("第一句", "zh")
            time.sleep(0.45)
            yield VoiceUtterance("第二句", "zh")

        merged = list(iter_aggregated_utterances(source(), stop_event=stop))
        self.assertEqual(len(merged), 2)


if __name__ == "__main__":
    unittest.main()
