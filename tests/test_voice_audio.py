import unittest
from unittest.mock import patch

from xagent.voice import audio as voice_audio


class _FakeDefaults:
    def __init__(self, device):
        self.device = device


class _FakeSoundDevice:
    def __init__(self):
        self.default = _FakeDefaults(device=(-1, 0))
        self._devices = [
            {
                "name": "vc4-hdmi-0",
                "hostapi": 0,
                "max_input_channels": 0,
                "max_output_channels": 2,
                "default_samplerate": 48000,
            },
            {
                "name": "vc4-hdmi-1",
                "hostapi": 0,
                "max_input_channels": 0,
                "max_output_channels": 2,
                "default_samplerate": 48000,
            },
            {
                "name": "UGREEN Camera 2K",
                "hostapi": 0,
                "max_input_channels": 2,
                "max_output_channels": 2,
                "default_samplerate": 48000,
            },
        ]

    def query_devices(self):
        return list(self._devices)

    def query_hostapis(self):
        return [{"name": "ALSA"}]

    def check_input_settings(self, *, device, channels, samplerate, dtype):
        del dtype
        if device == 2 and channels == 2 and samplerate == 16000:
            return None
        raise ValueError("unsupported input settings")

    def check_output_settings(self, *, device, channels, samplerate, dtype):
        del dtype
        if channels != 2 or samplerate != 48000:
            raise ValueError("unsupported output settings")
        if device in {0, 1, 2}:
            return None
        raise ValueError("unknown output device")


class VoiceAudioTests(unittest.TestCase):
    def test_resolve_audio_profile_prefers_duplex_usb_device(self):
        fake_sd = _FakeSoundDevice()

        with patch("xagent.voice.audio._import_sounddevice", return_value=fake_sd):
            profile = voice_audio.resolve_audio_io_profile(
                input_sample_rate=16000,
                input_channels=1,
                output_sample_rate=24000,
                output_channels=1,
            )

        self.assertEqual(profile.input_selection.device_index, 2)
        self.assertEqual(profile.input_selection.stream_channels, 2)
        self.assertEqual(profile.input_selection.stream_sample_rate, 16000)
        self.assertEqual(profile.output_selection.device_index, 2)
        self.assertEqual(profile.output_selection.stream_channels, 2)
        self.assertEqual(profile.output_selection.stream_sample_rate, 48000)

    def test_input_converter_downmixes_stereo_to_mono(self):
        converter = voice_audio._PCMInputConverter(
            source_channels=2,
            source_rate=16000,
            target_channels=1,
            target_rate=16000,
        )

        stereo_chunk = (
            (1000).to_bytes(2, byteorder="little", signed=True)
            + (-1000).to_bytes(2, byteorder="little", signed=True)
        ) * 4
        converted = converter.convert(stereo_chunk)

        self.assertEqual(len(converted), len(stereo_chunk) // 2)

    def test_output_converter_resamples_and_expands_to_stereo(self):
        converter = voice_audio._PCMOutputConverter(
            source_channels=1,
            source_rate=24000,
            target_channels=2,
            target_rate=48000,
        )

        mono_chunk = (500).to_bytes(2, byteorder="little", signed=True) * 16
        converted = converter.convert(mono_chunk)

        self.assertGreater(len(converted), len(mono_chunk))
        self.assertEqual(len(converted) % 4, 0)


if __name__ == "__main__":
    unittest.main()