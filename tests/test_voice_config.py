import unittest

from xagent.interfaces.cli.config_editor import prepare_voice_preset_update
from xagent.interfaces.cli.setup import VoiceInitSelection, _voice_channel_config
from xagent.interfaces.voice.config import VoiceChannelConfig, parse_quiet_hours


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

    def test_names_alias_merges_into_context_terms(self):
        config = VoiceChannelConfig.from_dict(
            {"api_key": "k", "names": ["Alice"], "context": {"terms": ["Bob"]}}
        )
        self.assertEqual(config.context.terms, ["Bob", "Alice"])

    def test_interruptions_alias(self):
        config = VoiceChannelConfig.from_dict({"api_key": "k", "interruptions": True})
        self.assertTrue(config.enable_interruptions)

    def test_quiet_hours_and_idle_shutdown_aliases(self):
        config = VoiceChannelConfig.from_dict(
            {
                "api_key": "k",
                "quiet_hours": "23:00-06:00",
                "idle_shutdown_minutes": 10,
            }
        )
        self.assertEqual(config.proactive.quiet_hours_start, 23)
        self.assertEqual(config.proactive.quiet_hours_end, 6)
        self.assertEqual(config.presence.close_stt_after_idle_seconds, 600.0)

    def test_return_timestamps_always_enabled(self):
        config = VoiceChannelConfig.from_dict({"api_key": "k", "return_timestamps": False})
        self.assertTrue(config.return_timestamps)

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
                "idle_shutdown_minutes": 5,
            }
        )
        public = original.to_public_dict()
        again = VoiceChannelConfig.from_dict(public)
        self.assertEqual(again.profile, "room")
        self.assertIn("Telos", again.context.terms)
        self.assertTrue(again.enable_interruptions)
        self.assertEqual(again.presence.close_stt_after_idle_seconds, 300.0)

    def test_voice_setup_preserves_advanced_keys(self):
        existing = {
            "api_key": "old",
            "profile": "room",
            "performance": {"max_agent_loops": 8},
            "context": {"general": [{"key": "domain", "value": "home"}]},
        }
        selection = VoiceInitSelection(voice_enabled=True, voice_api_key="new-key")
        merged = _voice_channel_config(selection, existing=existing)
        self.assertEqual(merged["api_key"], "new-key")
        self.assertEqual(merged["performance"]["max_agent_loops"], 8)
        self.assertEqual(merged["context"]["general"][0]["key"], "domain")

    def test_prepare_voice_preset_update_preserves_tuning(self):
        config = {
            "provider": {"name": "openai", "api_key": "k", "model": "gpt-5.6-terra"},
            "channels": {
                "voice": {
                    "api_key": "old",
                    "attention": {"open_window_seconds": 40},
                }
            },
        }
        update = prepare_voice_preset_update(config, provider="soniox", api_key="new")
        voice = update.data["channels"]["voice"]
        self.assertEqual(voice["api_key"], "new")
        self.assertEqual(voice["attention"]["open_window_seconds"], 40)

    def test_parse_quiet_hours(self):
        self.assertEqual(parse_quiet_hours("22:00-07:00"), (22, 7))
        self.assertEqual(parse_quiet_hours(""), (22, 7))


if __name__ == "__main__":
    unittest.main()
