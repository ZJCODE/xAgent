import unittest
from xagent.interfaces.voice import audio as voice_audio


class PCMConversionTests(unittest.TestCase):

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
