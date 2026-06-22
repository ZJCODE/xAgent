import unittest
from unittest.mock import patch

from xagent.interfaces.voice import audio as voice_audio


class _FakeDefaults:
    def __init__(self, device):
        self.device = device


class _FakeInputOutputPair:
    def __init__(self, input_index, output_index):
        self._values = [input_index, output_index]

    def __iter__(self):
        return iter(self._values)


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


class _FakeMacSoundDevice:
    def __init__(self):
        self.default = _FakeDefaults(device=_FakeInputOutputPair(2, 3))
        self._devices = [
            {
                "name": "Cast Audio",
                "hostapi": 0,
                "max_input_channels": 2,
                "max_output_channels": 2,
                "default_samplerate": 44100,
            },
            {
                "name": "Cast Audio (UI Sounds)",
                "hostapi": 0,
                "max_input_channels": 2,
                "max_output_channels": 2,
                "default_samplerate": 44100,
            },
            {
                "name": "iMac麦克风",
                "hostapi": 0,
                "max_input_channels": 1,
                "max_output_channels": 0,
                "default_samplerate": 48000,
            },
            {
                "name": "iMac扬声器",
                "hostapi": 0,
                "max_input_channels": 0,
                "max_output_channels": 2,
                "default_samplerate": 48000,
            },
            {
                "name": "NeCastAudio B",
                "hostapi": 0,
                "max_input_channels": 2,
                "max_output_channels": 2,
                "default_samplerate": 48000,
            },
        ]

    def query_devices(self):
        return list(self._devices)

    def query_hostapis(self):
        return ({"name": "Core Audio", "default_input_device": 2, "default_output_device": 3},)

    def check_input_settings(self, *, device, channels, samplerate, dtype):
        del dtype
        if (device, channels, samplerate) in {
            (0, 1, 16000),
            (1, 1, 16000),
            (2, 1, 16000),
            (4, 1, 16000),
        }:
            return None
        raise ValueError("unsupported input settings")

    def check_output_settings(self, *, device, channels, samplerate, dtype):
        del dtype
        if (device, channels, samplerate) in {
            (0, 1, 24000),
            (1, 1, 24000),
            (3, 1, 24000),
            (4, 1, 24000),
        }:
            return None
        raise ValueError("unsupported output settings")


class _FakeStereoUsbSoundDevice:
    def __init__(self):
        self.default = _FakeDefaults(device=(0, 0))
        self._devices = [
            {
                "name": "reSpeaker XVF3800 4-Mic Array: USB Audio (hw:0,0)",
                "hostapi": 0,
                "max_input_channels": 2,
                "max_output_channels": 2,
                "default_samplerate": 16000,
            },
        ]

    def query_devices(self):
        return list(self._devices)

    def query_hostapis(self):
        return [{"name": "ALSA"}]

    def check_input_settings(self, *, device, channels, samplerate, dtype):
        del device, dtype
        if channels in {1, 2} and samplerate == 16000:
            return None
        raise ValueError("unsupported input settings")

    def check_output_settings(self, *, device, channels, samplerate, dtype):
        del device, dtype
        if channels in {1, 2} and samplerate in {16000, 24000}:
            return None
        raise ValueError("unsupported output settings")


class VoiceAudioTests(unittest.TestCase):
    def test_default_device_indices_accept_sounddevice_pair(self):
        fake_sd = type("FakeSD", (), {"default": _FakeDefaults(device=_FakeInputOutputPair(2, 3))})()

        self.assertEqual(voice_audio._default_device_indices(fake_sd), (2, 3))

    def test_query_audio_devices_preserves_hostapi_zero_and_defaults(self):
        fake_sd = _FakeMacSoundDevice()

        devices = voice_audio._query_audio_devices(fake_sd)

        self.assertEqual(devices[0].hostapi_name, "Core Audio")
        self.assertTrue(devices[2].is_default_input)
        self.assertTrue(devices[3].is_default_output)

    def test_resolve_audio_profile_prefers_duplex_usb_device(self):
        fake_sd = _FakeSoundDevice()

        with patch("xagent.interfaces.voice.audio._import_sounddevice", return_value=fake_sd):
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

    def test_resolve_audio_profile_prefers_default_mac_builtins(self):
        fake_sd = _FakeMacSoundDevice()

        with patch("xagent.interfaces.voice.audio._import_sounddevice", return_value=fake_sd):
            profile = voice_audio.resolve_audio_io_profile(
                input_sample_rate=16000,
                input_channels=1,
                output_sample_rate=24000,
                output_channels=1,
            )

        self.assertEqual(profile.input_selection.device_index, 2)
        self.assertEqual(profile.input_selection.device_name, "iMac麦克风")
        self.assertEqual(profile.output_selection.device_index, 3)
        self.assertEqual(profile.output_selection.device_name, "iMac扬声器")

    def test_resolve_audio_profile_prefers_natural_stereo_stream_for_stereo_usb_device(self):
        fake_sd = _FakeStereoUsbSoundDevice()

        with patch("xagent.interfaces.voice.audio._import_sounddevice", return_value=fake_sd):
            profile = voice_audio.resolve_audio_io_profile(
                input_sample_rate=16000,
                input_channels=1,
                output_sample_rate=24000,
                output_channels=1,
            )

        self.assertEqual(profile.input_selection.device_index, 0)
        self.assertEqual(profile.input_selection.stream_channels, 2)
        self.assertEqual(profile.input_selection.target_channels, 1)
        self.assertEqual(profile.output_selection.device_index, 0)
        self.assertEqual(profile.output_selection.stream_channels, 2)
        self.assertEqual(profile.output_selection.target_channels, 1)

    def test_resolve_audio_profile_honors_named_device_preferences(self):
        fake_sd = _FakeMacSoundDevice()

        with patch("xagent.interfaces.voice.audio._import_sounddevice", return_value=fake_sd):
            profile = voice_audio.resolve_audio_io_profile(
                input_sample_rate=16000,
                input_channels=1,
                output_sample_rate=24000,
                output_channels=1,
                input_device="Cast Audio",
                output_device="iMac扬声器",
            )

        self.assertEqual(profile.input_selection.device_index, 0)
        self.assertEqual(profile.input_selection.device_name, "Cast Audio")
        self.assertEqual(profile.output_selection.device_index, 3)
        self.assertEqual(profile.output_selection.device_name, "iMac扬声器")

    def test_resolve_audio_profile_accepts_index_preferences(self):
        fake_sd = _FakeMacSoundDevice()

        with patch("xagent.interfaces.voice.audio._import_sounddevice", return_value=fake_sd):
            profile = voice_audio.resolve_audio_io_profile(
                input_sample_rate=16000,
                input_channels=1,
                output_sample_rate=24000,
                output_channels=1,
                input_device=2,
                output_device="#4",
            )

        self.assertEqual(profile.input_selection.device_index, 2)
        self.assertEqual(profile.output_selection.device_index, 4)

    def test_resolve_audio_profile_rejects_wrong_direction_preference(self):
        fake_sd = _FakeMacSoundDevice()

        with patch("xagent.interfaces.voice.audio._import_sounddevice", return_value=fake_sd):
            with self.assertRaisesRegex(RuntimeError, "has no output channels"):
                voice_audio.resolve_audio_io_profile(
                    input_sample_rate=16000,
                    input_channels=1,
                    output_sample_rate=24000,
                    output_channels=1,
                    output_device="iMac麦克风",
                )

    def test_list_audio_devices_text_splits_input_and_output_devices(self):
        fake_sd = _FakeMacSoundDevice()

        with patch("xagent.interfaces.voice.audio._import_sounddevice", return_value=fake_sd):
            text = voice_audio.list_audio_devices_text()

        self.assertIn("Input devices:", text)
        self.assertIn("Output devices:", text)
        self.assertIn("auto  Best available input", text)
        self.assertIn("#2  iMac麦克风", text)
        self.assertIn("#3  iMac扬声器", text)

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
