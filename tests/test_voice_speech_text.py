import unittest

from xagent.interfaces.voice.speech_text import (
    ConversationLanguageTracker,
    StreamingTTSSanitizer,
    detect_dominant_language,
    sanitize_spoken_text,
)


class VoiceSpeechTextTests(unittest.TestCase):
    def test_detect_dominant_language_from_reply_text(self):
        self.assertEqual(
            detect_dominant_language("你好，今天怎么样？", hints=["zh", "en"], fallback="en"),
            "zh",
        )
        self.assertEqual(
            detect_dominant_language("Hello there.", hints=["zh", "en"], fallback="zh"),
            "en",
        )

    def test_conversation_language_tracker_hysteresis(self):
        tracker = ConversationLanguageTracker(fallback="zh", hints=["zh", "en"])
        self.assertEqual(tracker.language_before_turn(), "zh")
        tracker.observe_reply("Hello.")
        self.assertEqual(tracker.language_before_turn(), "zh")
        tracker.observe_reply("Hello again.")
        self.assertEqual(tracker.language_before_turn(), "en")

    def test_streaming_sanitizer_strips_markdown_across_deltas(self):
        sanitizer = StreamingTTSSanitizer(hold_chars=4)
        parts = sanitizer.feed("**hel")
        parts.extend(sanitizer.feed("lo**"))
        parts.extend(sanitizer.flush())
        self.assertEqual("".join(parts), "hello")

    def test_sanitize_spoken_text_replaces_urls(self):
        spoken = sanitize_spoken_text("See https://example.com/path for details.")
        self.assertIn("链接", spoken)
        self.assertNotIn("https://", spoken)


if __name__ == "__main__":
    unittest.main()
