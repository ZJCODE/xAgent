"""Short TTS previews use draft preferences without changing Agent state."""
import io
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from tests.test_agent_edit_setup import _write_agent
from xagent.interfaces.server.voice_settings import (
    VOICE_PREVIEW_MAX_BYTES, VOICE_PREVIEW_TEXT, VoicePreviewInput, register_voice_settings_routes,
)
from xagent.interfaces.web.server import WebClientServer


class FakePreviewTTS:
    instances = []
    audio = b'\x01\x00' * 2400
    error = None
    stalled = False

    def __init__(self, **kwargs):
        self.config = kwargs['config']
        self.key = kwargs['api_key']
        self.cancelled = self.closed = False
        self.calls = []
        self.instances.append(self)

    def synthesize_chunks(self, text, **kwargs):
        self.calls.append((list(text), kwargs))
        if self.stalled:
            kwargs['stop_event'].wait(1)
        if self.error:
            raise self.error
        yield self.audio

    def cancel(self):
        self.cancelled = True

    def close(self):
        self.closed = True


class VoicePreviewTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.directory = _write_agent(self.root)
        config = yaml.safe_load((self.directory / 'config.yaml').read_text())
        config['channels']['voice'] = {'api_key': 'saved-key', 'voice': 'Daniel'}
        (self.directory / 'config.yaml').write_text(yaml.safe_dump(config))
        self.original = (self.directory / 'config.yaml').read_text()
        server = WebClientServer(host='127.0.0.1', port=1416, api_url='http://127.0.0.1:8010',
                                 config_dir=str(self.directory), registry_root=self.root)
        self.client = TestClient(server.app)
        FakePreviewTTS.instances = []
        FakePreviewTTS.error = None
        FakePreviewTTS.audio = b'\x01\x00' * 2400
        FakePreviewTTS.stalled = False
        mocked = patch('xagent.interfaces.voice.soniox.SonioxRealtimeTTS', FakePreviewTTS)
        mocked.start()
        self.addCleanup(mocked.stop)

    def preview(self, **kwargs):
        return self.client.post('/api/agent/voice/preview', json={'voice': 'Maya', 'speed': 1.25, **kwargs})

    def test_preview_is_wav_with_draft_voice_speed_and_saved_key_without_a_write(self):
        with patch('xagent.interfaces.server.admin_access.RuntimeClient') as runtime, patch(
            'xagent.interfaces.server.admin_access.RuntimeOwnership'
        ) as owner:
            response = self.preview()
            runtime.assert_not_called()
            owner.assert_not_called()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers['content-type'], 'audio/wav')
        self.assertEqual(response.headers['cache-control'], 'no-store')
        with wave.open(io.BytesIO(response.content), 'rb') as wav:
            self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth()), (24000, 1, 2))
            self.assertEqual(wav.readframes(wav.getnframes()), FakePreviewTTS.audio)
        synth = FakePreviewTTS.instances[0]
        self.assertEqual((synth.config.voice, synth.config.speed, synth.key), ('Maya', 1.25, 'saved-key'))
        self.assertEqual(synth.calls[0][0], [VOICE_PREVIEW_TEXT])
        self.assertNotIn('spoken_ledger', synth.calls[0][1])
        self.assertTrue(synth.closed and synth.cancelled)
        self.assertEqual((self.directory / 'config.yaml').read_text(), self.original)

    def test_new_key_is_temporary_and_environment_key_is_supported(self):
        response = self.preview(voice_api_key=' temporary-key ')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(FakePreviewTTS.instances[-1].key, 'temporary-key')
        self.assertEqual((self.directory / 'config.yaml').read_text(), self.original)
        config = yaml.safe_load(self.original)
        config['channels'].pop('voice')
        (self.directory / 'config.yaml').write_text(yaml.safe_dump(config))
        with patch.dict('os.environ', {'SONIOX_API_KEY': 'environment-key'}):
            self.assertEqual(self.preview().status_code, 200)
            self.assertEqual(FakePreviewTTS.instances[-1].key, 'environment-key')
        with patch.dict('os.environ', {'SONIOX_API_KEY': ''}):
            self.assertEqual(self.preview().status_code, 400)

    def test_invalid_preferences_and_custom_text_never_start_synthesis(self):
        for values in ({'voice': 'invalid'}, {'speed': True}, {'speed': .1}, {'text': 'custom'}):
            response = self.preview(**values)
            self.assertIn(response.status_code, (400, 422))
        self.assertEqual(FakePreviewTTS.instances, [])
        self.assertEqual((self.directory / 'config.yaml').read_text(), self.original)

    def test_failed_empty_and_oversized_audio_are_cleaned_up_without_leaking_provider_errors(self):
        for audio, error in ((b'', None), (b'\0' * (VOICE_PREVIEW_MAX_BYTES + 2), None),
                             (b'', RuntimeError('provider leaked secret-key'))):
            FakePreviewTTS.audio, FakePreviewTTS.error = audio, error
            response = self.preview()
            self.assertEqual(response.status_code, 502)
            self.assertNotIn('secret-key', response.text)
            self.assertTrue(FakePreviewTTS.instances[-1].closed)

    def test_timeout_stops_the_provider_and_does_not_write_preferences(self):
        FakePreviewTTS.stalled = True
        with patch('xagent.interfaces.server.voice_settings.VOICE_PREVIEW_TIMEOUT_SECONDS', .01):
            response = self.preview()
        self.assertEqual(response.status_code, 504)
        self.assertTrue(FakePreviewTTS.instances[-1].closed)
        self.assertTrue(FakePreviewTTS.instances[-1].calls[0][1]['stop_event'].is_set())
        self.assertEqual((self.directory / 'config.yaml').read_text(), self.original)


class VoicePreviewDisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnecting_cancels_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'config.yaml').write_text('channels:\n  voice:\n    api_key: key\n')
            app = FastAPI()
            register_voice_settings_routes(app, lambda: SimpleNamespace(config_dir=root), lambda: None)
            endpoint = next(route.endpoint for route in app.routes if route.path == '/api/agent/voice/preview')
            FakePreviewTTS.stalled = True
            FakePreviewTTS.error = None
            FakePreviewTTS.instances = []
            with patch('xagent.interfaces.voice.soniox.SonioxRealtimeTTS', FakePreviewTTS):
                with self.assertRaises(HTTPException) as error:
                    await endpoint(VoicePreviewInput(voice='Maya', speed=1), SimpleNamespace(is_disconnected=AsyncMock(return_value=True)))
            self.assertEqual(error.exception.status_code, 499)
            self.assertTrue(FakePreviewTTS.instances[0].closed)
