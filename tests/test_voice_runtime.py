import asyncio
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from xagent.core.runtime import (
    ContactEntry,
    SubconsciousDelivery,
    enqueue_scheduled_task,
    list_active_task_records,
)
from xagent.interfaces.voice.config import (
    SONIOX_ENDPOINT_LATENCY_LEVEL,
    SONIOX_ENDPOINT_SENSITIVITY,
    SONIOX_MAX_ENDPOINT_DELAY_MS,
    SONIOX_STT_MODEL,
    SONIOX_TTS_MAX_TEXT_CHARS,
    SONIOX_TTS_MODEL,
    VoiceChannelConfig,
    VoiceRuntimeProfile,
    VoiceSpeechStyle,
)
from xagent.interfaces.voice.audio import AudioTopology
from xagent.interfaces.voice.factory import create_local_voice_runtime
from xagent.interfaces.voice.floor import FloorState
from xagent.interfaces.voice.spoken_ledger import SpokenLedger
from xagent.interfaces.voice.runtime import VoiceRuntime, VoiceRuntimeOptions
from xagent.interfaces.voice.types import VoiceUtterance
from xagent.interfaces.voice.soniox import (
    SonioxRealtimeSTT,
    SonioxRealtimeTTS,
    SonioxVoiceError,
    _split_text_chunk,
)


class FakeMicrophone:
    def iter_chunks(self, *, pause_event: threading.Event, stop_event: threading.Event):
        del pause_event, stop_event
        yield b"audio"


class FakeRecognizer:
    def __init__(self, utterances):
        self.utterances = list(utterances)

    def iter_utterances(self, audio_chunks, *, pause_event: threading.Event, stop_event: threading.Event):
        del pause_event, stop_event
        list(audio_chunks)
        yield from self.utterances


class FakeSynthesizer:
    def __init__(self):
        self.calls = []
        self.cancelled = False

    def synthesize_chunks(self, text_chunks, *, language: str, stop_event: threading.Event, **kwargs):
        del stop_event, kwargs
        chunks = list(text_chunks)
        self.calls.append({"language": language, "chunks": chunks})
        for chunk in chunks:
            yield chunk.encode()

    def cancel(self):
        self.cancelled = True


class FailingOnceSynthesizer(FakeSynthesizer):
    def synthesize_chunks(self, text_chunks, *, language: str, stop_event: threading.Event, **kwargs):
        del kwargs
        chunks = list(text_chunks)
        self.calls.append({"language": language, "chunks": chunks})
        if len(self.calls) == 1:
            raise RuntimeError("tts unavailable")
        yield from (chunk.encode() for chunk in chunks)


class FakePlayer:
    def __init__(self):
        self.played = []
        self.pause_was_set = False
        self.pause_event = None

    def play_chunks(self, chunks, *, stop_event: threading.Event):
        del stop_event
        if self.pause_event is not None:
            self.pause_was_set = self.pause_event.is_set()
        self.played.extend(chunks)


class FakeAgent:
    async def chat_events(self, **kwargs):
        self.kwargs = kwargs
        yield {"type": "message_delta", "message_id": "m1", "delta": "hello "}
        yield {"type": "message_delta", "message_id": "m1", "delta": "there."}
        yield {"type": "message_done", "message_id": "m1", "content": "hello there."}


class FailingFirstAgent:
    def __init__(self):
        self.calls = 0

    async def chat_events(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield {"type": "error", "error": "model unavailable"}
            return
        yield {"type": "message_done", "message_id": str(self.calls), "content": "recovered"}


def voice_config(data=None, *, profile="headset", echo_managed=False):
    payload = {"api_key": "test-key", **(data or {})}
    config = VoiceChannelConfig.from_dict(payload)
    config.apply_runtime_profile(
        VoiceRuntimeProfile(name=profile, echo_managed=echo_managed, source="test")
    )
    return config


class VoiceConfigTests(unittest.TestCase):
    def test_minimal_configuration_uses_fixed_defaults(self):
        config = VoiceChannelConfig.from_dict({"api_key": "key"})

        self.assertEqual(config.voice, "Daniel")
        self.assertEqual(config.languages, ["zh", "en"])
        self.assertEqual(config.fallback_language, "zh")
        self.assertEqual(config.speed, 1.0)
        self.assertEqual(config.audio.input, "auto")

    def test_speech_style_comes_from_the_agent_not_the_channel(self):
        config = VoiceChannelConfig.from_dict({"api_key": "key"})
        config.apply_speech_style(VoiceSpeechStyle(voice="Ava", speed=1.2))

        self.assertEqual(config.voice, "Ava")
        self.assertEqual(config.speed, 1.2)

    def test_accepts_voice_name_rejects_speed_key(self):
        config = VoiceChannelConfig.from_dict({"api_key": "key", "voice": "Daniel"})
        self.assertEqual(config.speaking_voice, "Daniel")
        self.assertEqual(config.to_public_dict()["voice"], "Daniel")
        with self.assertRaisesRegex(ValueError, "non-empty"):
            VoiceChannelConfig.from_dict({"api_key": "key", "voice": "  "})
        with self.assertRaisesRegex(ValueError, "Unknown voice setting"):
            VoiceChannelConfig.from_dict({"api_key": "key", "speed": 1.2})

    def test_full_flat_configuration(self):
        config = VoiceChannelConfig.from_dict(
            {
                "api_key": " key ",
                "languages": ["en", "zh", "en"],
                "audio": {"input": "Mic", "output": 2},
            }
        )

        self.assertEqual(config.api_key, "key")
        self.assertEqual(config.languages, ["en", "zh"])
        self.assertEqual(config.fallback_language, "en")
        self.assertEqual(config.audio.output, 2)

    def test_api_key_falls_back_to_environment(self):
        with patch.dict(os.environ, {"SONIOX_API_KEY": "environment-key"}):
            config = VoiceChannelConfig.from_dict({})
            self.assertEqual(config.resolved_api_key(), "environment-key")

    def test_placeholder_falls_back_to_environment(self):
        with patch.dict(os.environ, {"SONIOX_API_KEY": "environment-key"}):
            config = VoiceChannelConfig.from_dict({"api_key": "your_soniox_api_key_here"})
            self.assertEqual(config.resolved_api_key(), "environment-key")

    def test_missing_credentials_fail(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "SONIOX_API_KEY"):
                VoiceChannelConfig.from_dict({}).resolved_api_key()

    def test_rejects_empty_languages(self):
        with self.assertRaisesRegex(ValueError, "languages"):
            VoiceChannelConfig.from_dict({"languages": [" "]})

    def test_interruptions_follow_the_detected_devices(self):
        config = VoiceChannelConfig.from_dict({"api_key": "key"})
        self.assertFalse(config.enable_interruptions)
        config.apply_runtime_profile(
            VoiceRuntimeProfile(name="room", echo_managed=True, source="test")
        )
        self.assertTrue(config.enable_interruptions)
        self.assertTrue(config.return_timestamps)

    def test_rejects_profile_key(self):
        with self.assertRaisesRegex(ValueError, "Unknown voice setting"):
            VoiceChannelConfig.from_dict({"api_key": "key", "profile": "headset"})

    def test_rejects_unknown_voice_keys(self):
        with self.assertRaisesRegex(ValueError, "Unknown voice setting"):
            VoiceChannelConfig.from_dict({"api_key": "key", "enable_interruptions": True})


class FakeSTTSession:
    def __init__(self, events):
        self.events = events
        self.sent = []
        self.pause_calls = []
        self.resume_calls = 0
        self.closed = False
        self.audio_sent = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def send_byte_chunk(self, chunk):
        self.sent.append(chunk)
        self.audio_sent.set()

    def pause(self, *, finalize=True):
        self.pause_calls.append(finalize)

    def resume(self):
        self.resume_calls += 1

    def receive_events(self):
        self.audio_sent.wait(0.5)
        yield from self.events

    def close(self):
        self.closed = True


class FakeTTSConnection:
    def __init__(self, audio=(b"one", b"two")):
        self.audio = audio
        self.sent = []
        self.finished = threading.Event()
        self.cancelled = 0
        self.closed = False
        self.keepalive = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def send_text_chunk(self, text, *, text_end=False):
        self.sent.append((text, text_end))

    def finish(self):
        self.finished.set()

    def cancel(self):
        self.cancelled += 1
        self.finished.set()

    def receive_audio_chunks(self):
        self.finished.wait(1.0)
        if not self.cancelled:
            yield from self.audio

    def close(self):
        self.closed = True
        self.finished.set()


class FakeTTSEvent:
    def __init__(self, *, audio=None, error_code=None, terminated=False):
        self.audio = None
        self._audio_bytes = audio
        self.error_code = error_code
        self.terminated = terminated

    def audio_bytes(self):
        return self._audio_bytes


class FakeTTSStream:
    def __init__(self, connection, config):
        self.connection = connection
        self.config = config

    def send_text_chunk(self, text, *, text_end=False):
        self.connection.send_text_chunk(text, text_end=text_end)

    def finish(self):
        self.connection.finish()

    def cancel(self):
        self.connection.cancel()

    def keep_alive(self):
        self.connection.keepalive += 1

    def receive_events(self):
        self.connection.finished.wait(1.0)
        if not self.connection.cancelled:
            for chunk in self.connection.audio:
                yield FakeTTSEvent(audio=chunk, terminated=False)
        yield FakeTTSEvent(terminated=True)


class FakeTTSMux:
    def __init__(self, connection):
        self.connection = connection
        self.configs = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def open_stream(self, *, config):
        self.configs.append(config)
        return FakeTTSStream(self.connection, config)

    def keep_alive(self):
        self.connection.keepalive += 1

    def close(self):
        self.connection.close()


class FakeRealtimeEndpoint:
    def __init__(self, session):
        self.session = session
        self.configs = []

    def connect(self, *, config):
        self.configs.append(config)
        return self.session


class FakeTTSEndpoint:
    def __init__(self, connection):
        self.connection = connection
        self.configs = []
        self.muxes = []

    def connect(self, *, config):
        self.configs.append(config)
        return self.connection

    def connect_multi_stream(self, **kwargs):
        del kwargs
        mux = FakeTTSMux(self.connection)
        self.muxes.append(mux)
        return mux


class FakeClient:
    def __init__(self, *, stt_session=None, tts_connection=None):
        self.realtime = SimpleNamespace(
            stt=FakeRealtimeEndpoint(stt_session),
            tts=FakeTTSEndpoint(tts_connection or FakeTTSConnection()),
        )


def token(text, *, final=True, language=None):
    return SimpleNamespace(text=text, is_final=final, language=language)


def event(*tokens, finished=False, error_code=None, error_type=None):
    return SimpleNamespace(
        tokens=list(tokens),
        finished=finished,
        error_code=error_code,
        error_message="failed" if error_code else None,
        model_extra={"error_type": error_type} if error_type else {},
    )


class SonioxSDKAdapterTests(unittest.TestCase):
    def test_stt_uses_fixed_sdk_configuration(self):
        session = FakeSTTSession([])
        client = FakeClient(stt_session=session)
        stt = SonioxRealtimeSTT(api_key="key", config=voice_config(), client=client)

        config = stt._stt_config()

        self.assertEqual(config.model, SONIOX_STT_MODEL)
        self.assertEqual(config.audio_format, "pcm_s16le")
        self.assertEqual(config.sample_rate, 16000)
        self.assertEqual(config.num_channels, 1)
        self.assertTrue(config.enable_endpoint_detection)
        self.assertEqual(config.endpoint_latency_adjustment_level, SONIOX_ENDPOINT_LATENCY_LEVEL)
        self.assertEqual(config.endpoint_sensitivity, 0.4)
        self.assertEqual(config.max_endpoint_delay_ms, 1_000)
        self.assertTrue(config.enable_language_identification)
        self.assertFalse(config.enable_speaker_diarization)

    def test_stt_aggregates_only_final_tokens_until_end_and_selects_main_language(self):
        session = FakeSTTSession(
            [
                event(token("draft", final=False, language="en")),
                event(
                    token("你", language="zh"),
                    token("好", language="zh"),
                    token("!", language="en"),
                    token("<end>", language="zh"),
                ),
            ]
        )
        stt = SonioxRealtimeSTT(
            api_key="key",
            config=voice_config(),
            client=FakeClient(stt_session=session),
        )

        utterances = list(
            stt._iter_session(
                [b"audio"],
                pause_event=threading.Event(),
                stop_event=threading.Event(),
                session_stop=threading.Event(),
            )
        )

        self.assertEqual(utterances, [VoiceUtterance(text="你好!", language="zh")])

    def test_stt_delegates_pause_resume_and_keepalive_to_sdk(self):
        pause_event = threading.Event()
        session = FakeSTTSession([event(finished=True)])

        def audio_chunks():
            pause_event.set()
            yield b""
            pause_event.clear()
            yield b"audio"

        stt = SonioxRealtimeSTT(
            api_key="key",
            config=voice_config(),
            client=FakeClient(stt_session=session),
        )
        list(
            stt._iter_session(
                audio_chunks(),
                pause_event=pause_event,
                stop_event=threading.Event(),
                session_stop=threading.Event(),
            )
        )

        self.assertEqual(session.pause_calls, [False])
        self.assertEqual(session.resume_calls, 1)
        self.assertEqual(session.sent, [b"audio"])

    def test_stt_reconnects_for_service_and_max_duration_errors(self):
        stt = SonioxRealtimeSTT(
            api_key="key",
            config=voice_config(),
            client=FakeClient(stt_session=FakeSTTSession([])),
        )
        utterance = VoiceUtterance("next", "en")
        for error_type in ("service_unavailable", "max_duration_reached", "internal_error"):
            with self.subTest(error_type=error_type):
                with patch.object(
                    stt,
                    "_iter_session",
                    side_effect=[
                        SonioxVoiceError("retry", error_type=error_type),
                        iter([utterance]),
                    ],
                ), patch("xagent.interfaces.voice.soniox.STT_RECONNECT_BASE_SECONDS", 0):
                    iterator = stt.iter_utterances(
                        [],
                        pause_event=threading.Event(),
                        stop_event=threading.Event(),
                    )
                    self.assertEqual(next(iterator), utterance)
                    iterator.close()

    def test_stt_exposes_nonrecoverable_errors(self):
        stt = SonioxRealtimeSTT(
            api_key="key",
            config=voice_config(),
            client=FakeClient(stt_session=FakeSTTSession([])),
        )
        with patch.object(
            stt,
            "_iter_session",
            side_effect=SonioxVoiceError("unauthorized", error_type="unauthorized"),
        ):
            with self.assertRaisesRegex(SonioxVoiceError, "unauthorized"):
                next(
                    stt.iter_utterances(
                        [],
                        pause_event=threading.Event(),
                        stop_event=threading.Event(),
                    )
                )

    def test_tts_does_not_connect_before_nonempty_text(self):
        connection = FakeTTSConnection()
        client = FakeClient(tts_connection=connection)
        tts = SonioxRealtimeTTS(api_key="key", config=voice_config(), client=client)

        self.assertEqual(list(tts.synthesize_chunks([], language="zh", stop_event=threading.Event())), [])
        self.assertEqual(client.realtime.tts.muxes, [])

    def test_tts_sends_deltas_immediately_and_splits_only_at_api_limit(self):
        connection = FakeTTSConnection()
        client = FakeClient(tts_connection=connection)
        tts = SonioxRealtimeTTS(api_key="key", config=voice_config(), client=client)
        long_text = "a" * (SONIOX_TTS_MAX_TEXT_CHARS + 3)

        audio = list(
            tts.synthesize_chunks(
                ["one", "two", long_text],
                language="en",
                stop_event=threading.Event(),
            )
        )

        self.assertEqual(audio, [b"one", b"two"])
        self.assertEqual(connection.sent[0:2], [("one", False), ("two", False)])
        self.assertEqual(len(connection.sent[2][0]), SONIOX_TTS_MAX_TEXT_CHARS)
        self.assertEqual(connection.sent[3], ("aaa", False))
        sdk_config = client.realtime.tts.muxes[0].configs[0]
        self.assertEqual(sdk_config.model, SONIOX_TTS_MODEL)
        self.assertEqual(sdk_config.sample_rate, 24000)
        self.assertTrue(sdk_config.return_timestamps)

    def test_tts_cancels_when_stopped(self):
        stop_event = threading.Event()
        stop_event.set()
        connection = FakeTTSConnection()
        tts = SonioxRealtimeTTS(
            api_key="key",
            config=voice_config(),
            client=FakeClient(tts_connection=connection),
        )

        self.assertEqual(list(tts.synthesize_chunks(["hello"], language="en", stop_event=stop_event)), [])
        self.assertEqual(connection.cancelled, 0)

    def test_text_split_has_no_small_delta_buffering(self):
        self.assertEqual(list(_split_text_chunk("abc")), ["abc"])


class VoiceRuntimeTests(unittest.TestCase):
    def setUp(self):
        cooldown = patch("xagent.interfaces.voice.runtime._PLAYBACK_MICROPHONE_COOLDOWN_SECONDS", 0.0)
        cooldown.start()
        self.addCleanup(cooldown.stop)

    def make_runtime(
        self,
        *,
        agent=None,
        recognizer=None,
        synthesizer=None,
        player=None,
        tasks_dir=None,
        config=None,
        allow_proactive_output=False,
    ):
        runtime = VoiceRuntime(
            agent=agent or FakeAgent(),
            config=config or voice_config(),
            microphone=FakeMicrophone(),
            recognizer=recognizer or FakeRecognizer([]),
            synthesizer=synthesizer or FakeSynthesizer(),
            player=player or FakePlayer(),
            options=VoiceRuntimeOptions(
                user_id="alice",
                tasks_dir=tasks_dir,
                allow_proactive_output=allow_proactive_output,
            ),
            output=lambda *args, **kwargs: None,
        )
        return runtime

    def test_voice_mode_does_not_start_autonomous_output_by_default(self):
        runtime = self.make_runtime()

        self.assertIsNone(runtime.task_scheduler)
        self.assertFalse(runtime._proactive_speech_allowed())

    def test_configured_interruptions_control_microphone_during_reply(self):
        for mode, detected, paused in (("on", False, False), ("off", True, True)):
            with self.subTest(mode=mode):
                player = FakePlayer()
                runtime = self.make_runtime(
                    config=voice_config({"interruptions": mode}, echo_managed=detected),
                    recognizer=FakeRecognizer([VoiceUtterance("你好", "zh")]),
                    player=player,
                )
                player.pause_event = runtime.pause_event
                asyncio.run(runtime.run_forever())
                self.assertTrue(player.played)
                self.assertEqual(player.pause_was_set, paused)

    def test_forced_interruptions_still_degrade_on_repeated_echo(self):
        async def run():
            runtime = self.make_runtime(config=voice_config({"interruptions": "on"}))
            runtime.recognizer.partial_relay = SimpleNamespace(snapshot=lambda: "hello there")
            runtime._floor.allow_duplex_capture = True
            stop = threading.Event()
            with patch("xagent.interfaces.voice.runtime.time", Mock(monotonic=Mock(side_effect=[0, 1, 2, 3]))), patch(
                "xagent.interfaces.voice.runtime.asyncio.sleep", new_callable=AsyncMock
            ), self.assertLogs("VoiceRuntime", level="WARNING") as logs:
                await runtime._monitor_barge_in(
                    playback_stop_event=stop,
                    spoken_ledger=SpokenLedger(),
                    reply_buffer=["hello there."],
                )
            self.assertIn("interruptions=on", " ".join(logs.output))
            self.assertFalse(runtime._duplex_capture_enabled)
            self.assertFalse(runtime._floor.allow_duplex_capture)
            self.assertTrue(runtime.pause_event.is_set())
            self.assertFalse(stop.is_set())
            self.assertEqual(runtime.config.to_public_dict()["interruptions"], "on")
            await runtime._release_microphone_after_playback()
            self.assertFalse(runtime.pause_event.is_set())
        asyncio.run(run())

    def test_forced_interruptions_cancel_reply_on_user_speech(self):
        async def run():
            agent = FakeAgent()
            agent.abort = Mock()
            runtime = self.make_runtime(agent=agent, config=voice_config({"interruptions": "on"}))
            runtime.recognizer.partial_relay = SimpleNamespace(snapshot=lambda: "wait stop")
            runtime._floor.state = FloorState.SPEAKING
            stop = threading.Event()
            interrupted = {"value": False}
            with patch("xagent.interfaces.voice.runtime.time", Mock(monotonic=Mock(side_effect=[0, 1]))), patch(
                "xagent.interfaces.voice.runtime.asyncio.sleep", new_callable=AsyncMock
            ):
                await runtime._monitor_barge_in(
                    playback_stop_event=stop,
                    spoken_ledger=SpokenLedger(),
                    reply_buffer=["hello there."],
                    interrupted_flag=interrupted,
                )
            self.assertTrue(stop.is_set())
            self.assertTrue(interrupted["value"])
            self.assertTrue(runtime.synthesizer.cancelled)
            agent.abort.assert_called_once()
            self.assertEqual(runtime._floor.state, FloorState.USER_SPEAKING)
            self.assertTrue(runtime._duplex_capture_enabled)
        asyncio.run(run())

    def test_routes_endpoint_to_agent_and_streamed_tts(self):
        agent = FakeAgent()
        synth = FakeSynthesizer()
        player = FakePlayer()
        runtime = self.make_runtime(
            agent=agent,
            recognizer=FakeRecognizer([VoiceUtterance("你好", "zh")]),
            synthesizer=synth,
            player=player,
        )
        player.pause_event = runtime.pause_event

        with self.assertLogs("VoiceRuntime", level="INFO") as logs:
            asyncio.run(runtime.run_forever())

        self.assertEqual(agent.kwargs["user_message"], "你好")
        self.assertIn("voice channel", agent.kwargs["channel_instructions"].lower())
        self.assertEqual(synth.calls[0], {"language": "en", "chunks": ["hello ", "there."]})
        self.assertEqual(player.played, [b"hello ", b"there."])
        self.assertTrue(player.pause_was_set)
        combined = "\n".join(logs.output)
        self.assertIn("endpoint_to_agent_first_text_ms", combined)
        self.assertIn("agent_first_text_to_tts_first_audio_ms", combined)
        self.assertIn("endpoint_to_first_audio_ms", combined)
        self.assertIn("turn_total_ms", combined)

    def test_uses_fallback_language_when_stt_has_none(self):
        synth = FakeSynthesizer()
        runtime = self.make_runtime(
            recognizer=FakeRecognizer([VoiceUtterance("hello")]),
            synthesizer=synth,
        )
        asyncio.run(runtime.run_forever())
        self.assertEqual(synth.calls[0]["language"], "en")

    def test_agent_failure_releases_microphone_and_continues(self):
        agent = FailingFirstAgent()
        synth = FakeSynthesizer()
        runtime = self.make_runtime(
            agent=agent,
            recognizer=FakeRecognizer([VoiceUtterance("one"), VoiceUtterance("two")]),
            synthesizer=synth,
        )

        def _no_aggregate(utterances, *, stop_event, grace_scale=1.0):
            del stop_event, grace_scale
            yield from utterances

        with patch(
            "xagent.interfaces.voice.runtime.iter_aggregated_utterances",
            side_effect=_no_aggregate,
        ):
            asyncio.run(runtime.run_forever())

        self.assertEqual(agent.calls, 2)
        self.assertEqual(synth.calls[-1]["chunks"], ["recovered"])
        self.assertFalse(runtime.pause_event.is_set())

    def test_tts_failure_releases_microphone_and_continues(self):
        synth = FailingOnceSynthesizer()
        runtime = self.make_runtime(
            recognizer=FakeRecognizer([VoiceUtterance("one"), VoiceUtterance("two")]),
            synthesizer=synth,
        )

        def _no_aggregate(utterances, *, stop_event, grace_scale=1.0):
            del stop_event, grace_scale
            yield from utterances

        with patch(
            "xagent.interfaces.voice.runtime.iter_aggregated_utterances",
            side_effect=_no_aggregate,
        ):
            asyncio.run(runtime.run_forever())

        self.assertGreaterEqual(len(synth.calls), 2)
        self.assertFalse(runtime.pause_event.is_set())

    def test_scheduled_message_uses_shared_speak_path(self):
        async def run_task():
            with tempfile.TemporaryDirectory() as tmpdir:
                enqueue_scheduled_task(
                    task_type="message",
                    content="drink water",
                    run_at=datetime.now() - timedelta(seconds=1),
                    tasks_dir=tmpdir,
                    channel="voice",
                    user_id="alice",
                    target={"user_id": "alice"},
                )
                runtime = self.make_runtime(tasks_dir=tmpdir, allow_proactive_output=True)
                runtime._speak = AsyncMock()
                await runtime.task_scheduler.tick()
                self.assertEqual(runtime._speak.await_count, 1)
                self.assertEqual(list_active_task_records(tmpdir), [])

        asyncio.run(run_task())

    def test_subconscious_delivery_uses_shared_speak_path(self):
        async def run_delivery():
            agent = FakeAgent()
            agent.message_handler = SimpleNamespace(store_model_reply=AsyncMock())
            runtime = self.make_runtime(agent=agent, allow_proactive_output=True)
            runtime._speak = AsyncMock()
            delivery = SubconsciousDelivery(
                content="background thought",
                recipient=ContactEntry(
                    channel="voice",
                    user_id="alice",
                    target={"user_id": "alice"},
                    last_seen="2026-08-09 10:00:00",
                ),
                internal_content="inner",
                created_at=datetime(2026, 8, 9, 10, 0, 0),
            )
            await runtime.deliver_subconscious_message(delivery)
            runtime._speak.assert_awaited_once()

        asyncio.run(run_delivery())

    def test_factory_uses_fixed_soniox_audio_profiles(self):
        input_selection = SimpleNamespace(
            device_index=1,
            device_name="Mic",
            stream_sample_rate=16000,
            stream_channels=1,
        )
        output_selection = SimpleNamespace(
            device_index=2,
            device_name="Speaker",
            stream_sample_rate=24000,
            stream_channels=1,
        )
        profile = SimpleNamespace(
            input_selection=input_selection,
            output_selection=output_selection,
            topology=AudioTopology(near_field=False, echo_managed=False, reason="test"),
        )
        with patch("xagent.interfaces.voice.factory.create_soniox_adapters", return_value=(object(), object())), patch(
            "xagent.interfaces.voice.factory.resolve_audio_io_profile", return_value=profile
        ) as resolve, patch("xagent.interfaces.voice.factory.SoundDeviceMicrophone") as microphone, patch(
            "xagent.interfaces.voice.factory.SoundDevicePlayer"
        ) as player:
            create_local_voice_runtime(
                agent=FakeAgent(),
                config=voice_config(),
                options=VoiceRuntimeOptions(),
            )

        self.assertEqual(resolve.call_args.kwargs["input_sample_rate"], 16000)
        self.assertEqual(resolve.call_args.kwargs["output_sample_rate"], 24000)
        microphone.assert_called_once()
        player.assert_called_once()

    def test_factory_settles_profile_and_speech_before_the_adapters_exist(self):
        selection = SimpleNamespace(
            device_index=1,
            device_name="AirPods Pro",
            stream_sample_rate=16000,
            stream_channels=1,
        )
        audio_profile = SimpleNamespace(
            input_selection=selection,
            output_selection=selection,
            topology=AudioTopology(
                near_field=True, echo_managed=True, reason="device name contains 'airpod'"
            ),
        )
        config = VoiceChannelConfig.from_dict({"api_key": "key", "interruptions": "off"})
        agent = FakeAgent()
        agent.voice = "Daniel"
        seen: dict[str, object] = {}

        def _adapters(cfg, **kwargs):
            del kwargs
            seen["profile"] = cfg.profile
            seen["diarization"] = cfg.enable_diarization
            seen["voice"] = cfg.voice
            seen["speed"] = cfg.speed
            seen["interruptions"] = cfg.enable_interruptions
            return object(), object()

        with patch(
            "xagent.interfaces.voice.factory.create_soniox_adapters", side_effect=_adapters
        ), patch(
            "xagent.interfaces.voice.factory.resolve_audio_io_profile", return_value=audio_profile
        ), patch("xagent.interfaces.voice.factory.SoundDeviceMicrophone"), patch(
            "xagent.interfaces.voice.factory.SoundDevicePlayer"
        ):
            create_local_voice_runtime(
                agent=agent,
                config=config,
                options=VoiceRuntimeOptions(),
                speed_override=1.1,
            )

        self.assertEqual(
            seen,
            {
                "profile": "headset", "diarization": False, "voice": "Daniel",
                "speed": 1.1, "interruptions": False,
            },
        )
        self.assertFalse(config.enable_interruptions)
        self.assertTrue(config.runtime_profile.echo_managed)
