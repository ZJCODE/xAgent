import unittest

from xagent.interfaces.cli.config_editor import prepare_voice_preset_update
from xagent.interfaces.cli.setup import VoiceInitSelection, _voice_channel_config
from xagent.interfaces.voice.config import VoiceChannelConfig, parse_quiet_hours
from xagent.interfaces.voice.presence import VOICE_STT_IDLE_SHUTDOWN_SECONDS


class VoiceConfigSurfaceTests(unittest.TestCase):
    def test_profile_room_enables_diarization(self):
        config = VoiceChannelConfig.from_dict({"api_key": "k", "profile": "room"})
        self.assertTrue(config.enable_diarization)
        self.assertTrue(config.attention.use_decide_participation)

    def test_profile_headset_disables_diarization(self):
        config = VoiceChannelConfig.from_dict({"api_key": "k", "profile": "headset"})
        self.assertFalse(config.enable_diarization)
        self.assertFalse(config.attention.use_decide_participation)
        self.assertEqual(config.interruption.min_words, 1)

    def test_names_list(self):
        config = VoiceChannelConfig.from_dict({"api_key": "k", "names": ["Alice", "Bob", "Alice"]})
        self.assertEqual(config.names, ["Alice", "Bob"])

    def test_quiet_hours(self):
        config = VoiceChannelConfig.from_dict({"api_key": "k", "quiet_hours": "23:00-06:00"})
        self.assertEqual(config.proactive.quiet_hours_start, 23)
        self.assertEqual(config.proactive.quiet_hours_end, 6)

    def test_idle_shutdown_is_always_on(self):
        config = VoiceChannelConfig.from_dict({"api_key": "k"})
        self.assertEqual(
            config.presence.close_stt_after_idle_seconds,
            VOICE_STT_IDLE_SHUTDOWN_SECONDS,
        )
        self.assertGreater(config.presence.close_stt_after_idle_seconds, 0.0)

    def test_rejects_idle_shutdown_key(self):
        with self.assertRaisesRegex(ValueError, "Unknown voice setting"):
            VoiceChannelConfig.from_dict({"api_key": "k", "idle_shutdown_minutes": 10})

    def test_rejects_legacy_nested_keys(self):
        with self.assertRaisesRegex(ValueError, "Unknown voice setting"):
            VoiceChannelConfig.from_dict({"api_key": "k", "context": {"terms": ["x"]}})

    def test_unknown_key_suggests_close_match(self):
        with self.assertRaisesRegex(ValueError, "interruptions"):
            VoiceChannelConfig.from_dict({"api_key": "k", "interuptions": True})

    def test_to_public_dict_round_trip(self):
        original = VoiceChannelConfig.from_dict(
            {
                "api_key": "secret",
                "profile": "room",
                "names": ["Telos"],
                "interruptions": True,
            }
        )
        public = original.to_public_dict()
        again = VoiceChannelConfig.from_dict(public)
        self.assertEqual(again.profile, "room")
        self.assertEqual(again.names, ["Telos"])
        self.assertTrue(again.interruptions)

    def test_voice_setup_preserves_tier1_when_rotating_key(self):
        existing = {
            "api_key": "old",
            "profile": "room",
            "voice": "Ava",
            "names": ["Telos"],
            "audio": {"input": "Mic", "output": "Speaker"},
        }
        selection = VoiceInitSelection(voice_enabled=True, voice_api_key="new-key", voice_name="Ava")
        merged = _voice_channel_config(selection, existing=existing)
        self.assertEqual(merged["api_key"], "new-key")
        self.assertEqual(merged["voice"], "Ava")
        self.assertEqual(merged["names"], ["Telos"])
        self.assertEqual(merged["audio"]["input"], "Mic")

    def test_prepare_voice_preset_update_preserves_block(self):
        config = {
            "provider": {"name": "openai", "api_key": "k", "model": "gpt-5.6-terra"},
            "channels": {
                "voice": {
                    "api_key": "old",
                    "voice": "Ava",
                    "names": ["Telos"],
                }
            },
        }
        update = prepare_voice_preset_update(config, provider="soniox", api_key="new")
        voice = update.data["channels"]["voice"]
        self.assertEqual(voice["api_key"], "new")
        self.assertEqual(voice["voice"], "Ava")
        self.assertEqual(voice["names"], ["Telos"])

    def test_parse_quiet_hours(self):
        self.assertEqual(parse_quiet_hours("22:00-07:00"), (22, 7))
        self.assertEqual(parse_quiet_hours(""), (0, 0))


if __name__ == "__main__":
    unittest.main()
