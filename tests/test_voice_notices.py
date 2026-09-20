import unittest

from xagent.interfaces.voice.notices import VoiceNoticeCatalog


class VoiceNoticeCatalogTests(unittest.TestCase):
    def test_does_not_repeat_the_same_line_twice_in_a_row(self):
        catalog = VoiceNoticeCatalog.default()
        first = catalog.next_line("not_understood")
        second = catalog.next_line("not_understood")
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first.text, second.text)


if __name__ == "__main__":
    unittest.main()
