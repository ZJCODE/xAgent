import unittest

from xagent.interfaces.voice.spoken_ledger import SpokenLedger


class VoiceSpokenLedgerTests(unittest.TestCase):
    def test_maps_playback_time_to_spoken_prefix(self):
        ledger = SpokenLedger(sample_rate=24_000, channels=1)
        ledger.ingest_timestamps(
            characters=["你", "好", "世", "界"],
            character_end_times_seconds=[0.2, 0.3, 0.8, 1.0],
        )
        ledger.note_playback_bytes(14_400)
        prefix = ledger.spoken_prefix("你好世界")
        self.assertIn("你好", prefix)

    def test_metadata_carries_spoken_through(self):
        ledger = SpokenLedger(sample_rate=24_000, channels=1)
        ledger.ingest_timestamps(
            characters=["a", "b"],
            character_end_times_seconds=[0.5, 1.0],
        )
        ledger.note_playback_bytes(24_000)
        meta = ledger.spoken_through_metadata("ab")
        voice = meta["voice"]
        assert isinstance(voice, dict)
        self.assertGreater(int(voice["spoken_through_chars"]), 0)


if __name__ == "__main__":
    unittest.main()
