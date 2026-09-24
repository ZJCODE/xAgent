import unittest
from types import SimpleNamespace

import yaml

from xagent.interfaces.cli.config_editor import prepare_voice_preset_update
from xagent.interfaces.cli.setup import VoiceInitSelection, _voice_channel_config
from xagent.interfaces.voice.config import (
    VoiceChannelConfig,
    VoiceRuntimeProfile,
)
from xagent.interfaces.voice.audio import AudioTopology
from xagent.interfaces.voice.factory import resolve_runtime_profile
from xagent.interfaces.voice.presence import VOICE_STT_IDLE_SHUTDOWN_SECONDS


def _audio_profile(*, near_field: bool, echo_managed: bool):
    return SimpleNamespace(
        topology=AudioTopology(
            near_field=near_field,
            echo_managed=echo_managed,
            reason="test",
        )
    )


class VoiceConfigSurfaceTests(unittest.TestCase):
    def test_room_is_the_default_profile(self):
        config = VoiceChannelConfig.from_dict({"api_key": "k"})
        self.assertEqual(config.profile, "room")
        self.assertTrue(config.enable_diarization)
        self.assertTrue(config.attention.use_decide_participation)

    def test_detected_headset_profile_disables_diarization(self):
        config = VoiceChannelConfig.from_dict({"api_key": "k"})
        config.apply_runtime_profile(
            VoiceRuntimeProfile(name="headset", echo_managed=True, source="test")
        )
        self.assertFalse(config.enable_diarization)
        self.assertFalse(config.attention.use_decide_participation)
        self.assertEqual(config.interruption.min_words, 1)

    def test_rejects_names_key(self):
        with self.assertRaisesRegex(ValueError, "Unknown voice setting"):
            VoiceChannelConfig.from_dict({"api_key": "k", "names": ["Alice"]})

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
            VoiceChannelConfig.from_dict({"api_key": "k", "interruptio": "auto"})

    def test_to_public_dict_round_trip(self):
        original = VoiceChannelConfig.from_dict(
            {
                "api_key": "secret",
                "languages": ["en"],
            }
        )
        public = original.to_public_dict()
        again = VoiceChannelConfig.from_dict(public)
        self.assertEqual(again.languages, ["en"])

    def test_public_dict_is_the_curated_surface(self):
        public = VoiceChannelConfig.from_dict({"api_key": "secret"}).to_public_dict()

        self.assertEqual(
            public,
            {
                "api_key": "secret",
                "languages": ["zh", "en"],
                "voice": "Daniel",
                "interruptions": "auto",
            },
        )

    def test_public_dict_writes_audio_only_once_pinned(self):
        public = VoiceChannelConfig.from_dict(
            {"api_key": "secret", "audio": {"input": "Mic"}}
        ).to_public_dict()

        self.assertEqual(public["audio"], {"input": "Mic"})

    def test_voice_setup_preserves_tier1_when_rotating_key(self):
        existing = {
            "api_key": "old",
            "interruptions": "off",
            "audio": {"input": "Mic", "output": "Speaker"},
        }
        selection = VoiceInitSelection(voice_api_key="new-key")
        merged = _voice_channel_config(selection, existing=existing)
        self.assertEqual(merged["api_key"], "new-key")
        self.assertEqual(merged["interruptions"], "off")
        self.assertEqual(merged["audio"]["input"], "Mic")

    def test_prepare_voice_preset_update_preserves_block(self):
        config = {
            "provider": {"name": "openai", "api_key": "k", "model": "gpt-5.6-terra"},
            "channels": {
                "voice": {
                    "api_key": "old",
                    "interruptions": "on",
                    "audio": {"input": "Mic", "output": "Speaker"},
                }
            },
        }
        update = prepare_voice_preset_update(config, provider="soniox", api_key="new")
        voice = update.data["channels"]["voice"]
        self.assertEqual(voice["api_key"], "new")
        self.assertEqual(voice["interruptions"], "on")
        self.assertEqual(voice["audio"]["input"], "Mic")

    def test_runtime_profile_follows_detected_topology(self):
        room = resolve_runtime_profile(_audio_profile(near_field=False, echo_managed=False))
        self.assertEqual(room.name, "room")
        self.assertFalse(room.echo_managed)

        headset = resolve_runtime_profile(_audio_profile(near_field=True, echo_managed=True))
        self.assertEqual(headset.name, "headset")
        self.assertTrue(headset.echo_managed)

    def test_session_overrides_beat_detection(self):
        profile = resolve_runtime_profile(
            _audio_profile(near_field=True, echo_managed=False),
            profile_override="room",
            interruptions_override="on",
        )
        self.assertEqual(profile.name, "room")
        self.assertFalse(profile.echo_managed)
        self.assertEqual(profile.interruptions_override, "on")
        self.assertIn("override", profile.source)

    def test_interruption_precedence_preserves_hardware_and_saved_preference(self):
        for detected in (False, True):
            for persisted in ("auto", "on", "off"):
                for override in (None, "auto", "on", "off"):
                    with self.subTest(detected=detected, persisted=persisted, override=override):
                        config = VoiceChannelConfig.from_dict({"interruptions": persisted})
                        config.apply_runtime_profile(resolve_runtime_profile(
                            _audio_profile(near_field=False, echo_managed=detected),
                            interruptions_override=override,
                        ))
                        mode = persisted if override is None else override
                        self.assertEqual(config.interruption_mode, mode)
                        self.assertEqual(
                            config.enable_interruptions,
                            detected if mode == "auto" else mode == "on",
                        )
                        self.assertEqual(config.runtime_profile.echo_managed, detected)
                        self.assertEqual(config.to_public_dict()["interruptions"], persisted)

    def test_interruption_yaml_round_trip(self):
        for literal, expected in (
            ("auto", "auto"), ("on", "on"), ("off", "off"),
            ('"on"', "on"), ('"off"', "off"), ("true", "on"), ("false", "off"),
        ):
            with self.subTest(literal=literal):
                config = VoiceChannelConfig.from_dict(yaml.safe_load(f"interruptions: {literal}"))
                self.assertEqual(config.interruptions, expected)
                restored = VoiceChannelConfig.from_dict(yaml.safe_load(yaml.safe_dump(config.to_public_dict())))
                self.assertEqual(restored.interruptions, expected)

    def test_invalid_interruption_modes_are_rejected(self):
        for value in ("always", "", None, 0, 1, {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "interruptions"):
                VoiceChannelConfig.from_dict({"interruptions": value})


if __name__ == "__main__":
    unittest.main()
