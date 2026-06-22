import asyncio
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from xagent.application import current_delivery_context, enqueue_scheduled_task, list_active_task_records
from xagent.infrastructure.voice.config import VoiceChannelConfig, VoiceTTSConfig
from xagent.infrastructure.voice.factory import create_local_voice_runtime
from xagent.infrastructure.voice.qwen import (
    QwenRealtimeSTT,
    QwenRealtimeTTS,
    QwenVoiceError,
    _connect_qwen_websocket,
    _qwen_realtime_url,
)
from xagent.channels.voice.runtime import VoiceRuntime, VoiceRuntimeOptions, VoiceUtterance
from xagent.infrastructure.voice.soniox import SonioxRealtimeTTS, SonioxVoiceError, _batch_text_chunks, _connect_websocket


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

    def synthesize_chunks(self, text_chunks, *, language: str, stop_event: threading.Event):
        del stop_event
        chunks = list(text_chunks)
        self.calls.append({"language": language, "chunks": chunks})
        for chunk in chunks:
            yield chunk.encode("utf-8")

    def cancel(self):
        self.cancelled = True


class FakePlayer:
    def __init__(self):
        self.played = []

    def play_chunks(self, chunks, *, stop_event: threading.Event):
        del stop_event
        self.played.extend(chunks)


class StopAwareSynthesizer:
    def __init__(self):
        self.cancelled = False
        self.stop_event_was_set = False

    def synthesize_chunks(self, text_chunks, *, language: str, stop_event: threading.Event):
        del language
        for text in text_chunks:
            if "first" not in text:
                yield text.encode("utf-8")
                continue
            yield b"started"
            deadline = time.monotonic() + 1.0
            while not stop_event.is_set() and time.monotonic() < deadline:
                time.sleep(0.005)
            self.stop_event_was_set = self.stop_event_was_set or stop_event.is_set()
            if stop_event.is_set():
                return
            yield b"should-not-play"

    def cancel(self):
        self.cancelled = True


class StopAwarePlayer:
    def __init__(self):
        self.played = []
        self.stop_event_was_set = False

    def play_chunks(self, chunks, *, stop_event: threading.Event):
        for chunk in chunks:
            self.played.append(chunk)
            if stop_event.is_set():
                self.stop_event_was_set = True
                break
        self.stop_event_was_set = self.stop_event_was_set or stop_event.is_set()


class PauseAwarePlayer:
    def __init__(self, pause_event: threading.Event, *, wait_for_clear: bool = False):
        self.pause_event = pause_event
        self.wait_for_clear = wait_for_clear
        self.pause_was_set = False
        self.pause_was_cleared = False
        self.played = []

    def play_chunks(self, chunks, *, stop_event: threading.Event):
        self.pause_was_set = self.pause_event.is_set()
        if self.wait_for_clear:
            deadline = time.monotonic() + 1.0
            while self.pause_event.is_set() and time.monotonic() < deadline and not stop_event.is_set():
                time.sleep(0.005)
        self.pause_was_cleared = not self.pause_event.is_set()
        self.played.extend(chunks)


class InterruptRecognizer:
    def __init__(self):
        self.utterances = [
            VoiceUtterance(text="first", language="zh"),
            VoiceUtterance(text="interrupt", language="zh"),
        ]

    def iter_utterances(self, audio_chunks, *, pause_event: threading.Event, stop_event: threading.Event):
        del audio_chunks
        yield self.utterances[0]
        deadline = time.monotonic() + 1.0
        while pause_event.is_set() and time.monotonic() < deadline and not stop_event.is_set():
            time.sleep(0.005)
        yield self.utterances[1]


class BlockingRecognizer:
    def __init__(self):
        self.started = threading.Event()

    def iter_utterances(self, audio_chunks, *, pause_event: threading.Event, stop_event: threading.Event):
        del audio_chunks, pause_event
        self.started.set()
        while not stop_event.is_set():
            time.sleep(0.005)
        if False:
            yield VoiceUtterance(text="never")


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    def send(self, payload):
        self.sent.append(payload)


class FakeAgent:
    async def chat_events(self, **kwargs):
        self.kwargs = kwargs
        yield {"type": "message_start", "message_id": "m1", "phase": "final"}
        yield {"type": "message_delta", "message_id": "m1", "phase": "final", "delta": "hello "}
        yield {"type": "message_delta", "message_id": "m1", "phase": "final", "delta": "there."}
        yield {"type": "message_done", "message_id": "m1", "phase": "final", "content": "hello there."}
        yield {"type": "done"}


class RecordingAgent:
    def __init__(self):
        self.messages = []

    async def chat_events(self, **kwargs):
        self.messages.append(kwargs["user_message"])
        yield {"type": "message_done", "message_id": kwargs["user_message"], "phase": "final", "content": "ok"}
        yield {"type": "done"}


class ContextCapturingAgent:
    async def chat_events(self, **kwargs):
        self.kwargs = kwargs
        context = current_delivery_context()
        self.context = context
        yield {"type": "message_done", "message_id": "m1", "phase": "final", "content": "ok"}
        yield {"type": "done"}


class ScheduledAgent:
    async def chat_events(self, **kwargs):
        self.kwargs = kwargs
        context = current_delivery_context()
        self.context = context
        yield {"type": "message_delta", "message_id": "m1", "phase": "final", "delta": "done"}
        yield {"type": "message_done", "message_id": "m1", "phase": "final", "content": "done"}
        yield {"type": "done"}


class InterruptibleAgent:
    def __init__(self):
        self.messages = []

    async def chat_events(self, **kwargs):
        transcript = kwargs["user_message"]
        self.messages.append(transcript)
        yield {"type": "message_start", "message_id": transcript, "phase": "final"}
        yield {"type": "message_delta", "message_id": transcript, "phase": "final", "delta": f"{transcript} "}
        if transcript == "first":
            await asyncio.sleep(0.2)
        yield {"type": "message_done", "message_id": transcript, "phase": "final", "content": f"{transcript} done"}
        yield {"type": "done"}


def voice_channel_config(data):
    config = dict(data or {})
    api_key = config.pop("api_key", None)
    if api_key is not None:
        config.setdefault("stt", {}).setdefault("api_key", api_key)
        config.setdefault("tts", {}).setdefault("api_key", api_key)
    return VoiceChannelConfig.from_dict(config)


class VoiceRuntimeTests(unittest.TestCase):
    def test_voice_config_disables_interruptions_by_default(self):
        config = voice_channel_config({"provider": "qwen", "api_key": "qwen-key"})

        self.assertFalse(config.enable_interruptions)

    def test_voice_config_accepts_enabled_interruptions(self):
        config = voice_channel_config({
            "provider": "qwen",
            "api_key": "qwen-key",
            "enable_interruptions": True,
        })

        self.assertTrue(config.enable_interruptions)

    def test_voice_config_accepts_disabled_interruptions(self):
        config = voice_channel_config({
            "provider": "qwen",
            "api_key": "qwen-key",
            "enable_interruptions": False,
        })

        self.assertFalse(config.enable_interruptions)

    def test_voice_config_disables_wake_by_default(self):
        config = voice_channel_config({"provider": "qwen", "api_key": "qwen-key"})

        self.assertFalse(config.wake.enabled)
        self.assertEqual(config.wake.wake_phrases, ["xAgent"])
        self.assertEqual(config.wake.match_mode, "prefix")

    def test_voice_config_accepts_wake_phrases(self):
        config = voice_channel_config({
            "provider": "qwen",
            "api_key": "qwen-key",
            "wake": {
                "enabled": True,
                "wake_phrases": ["小智", "hey xAgent"],
                "match_mode": "contains",
                "idle_timeout_seconds": 15,
                "exit_phrases": ["退下"],
            },
        })

        self.assertTrue(config.wake.enabled)
        self.assertEqual(config.wake.wake_phrases, ["小智", "hey xAgent"])
        self.assertEqual(config.wake.match_mode, "contains")
        self.assertEqual(config.wake.idle_timeout_seconds, 15)
        self.assertEqual(config.wake.exit_phrases, ["退下"])

    def test_voice_config_rejects_enabled_wake_without_phrases(self):
        with self.assertRaisesRegex(ValueError, "voice.wake.wake_phrases"):
            voice_channel_config({
                "provider": "qwen",
                "api_key": "qwen-key",
                "wake": {
                    "enabled": True,
                    "wake_phrases": [],
                },
            })

    def test_voice_config_accepts_audio_device_preferences(self):
        config = voice_channel_config({
            "provider": "qwen",
            "api_key": "qwen-key",
            "audio": {
                "input": "MacBook Pro麦克风",
                "output": "#4",
            },
        })

        self.assertEqual(config.audio.input, "MacBook Pro麦克风")
        self.assertEqual(config.audio.output, "#4")

    def test_runtime_cancel_stops_waiting_for_blocked_recognizer(self):
        async def run_cancelled_runtime():
            config = voice_channel_config({"api_key": "test-key"})
            recognizer = BlockingRecognizer()
            runtime = VoiceRuntime(
                agent=FakeAgent(),
                config=config,
                microphone=FakeMicrophone(),
                recognizer=recognizer,
                synthesizer=FakeSynthesizer(),
                player=FakePlayer(),
                options=VoiceRuntimeOptions(user_id="alice"),
                output=lambda *args, **kwargs: None,
            )

            task = asyncio.create_task(runtime.run_forever())
            deadline = time.monotonic() + 1.0
            while not recognizer.started.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.005)
            self.assertTrue(recognizer.started.is_set())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=0.2)
            self.assertTrue(runtime.stop_event.is_set())

        asyncio.run(run_cancelled_runtime())

    def test_runtime_routes_soniox_endpoint_utterance_to_agent_and_tts(self):
        config = voice_channel_config({
            "api_key": "test-key",
            "tts": {
                "language_policy": "from_stt_dominant",
                "fallback_language": "zh",
            },
        })
        agent = FakeAgent()
        synth = FakeSynthesizer()
        player = FakePlayer()
        output_lines = []
        runtime = VoiceRuntime(
            agent=agent,
            config=config,
            microphone=FakeMicrophone(),
            recognizer=FakeRecognizer([VoiceUtterance(text="你好", language="zh")]),
            synthesizer=synth,
            player=player,
            options=VoiceRuntimeOptions(user_id="alice"),
            output=lambda *args, **kwargs: output_lines.append("".join(str(arg) for arg in args)),
        )

        asyncio.run(runtime.run_forever())

        self.assertEqual(agent.kwargs["user_id"], "alice")

        self.assertEqual(agent.kwargs["user_message"], "你好")
        self.assertEqual(synth.calls[0]["language"], "zh")
        self.assertEqual(synth.calls[0]["chunks"], ["hello ", "there."])
        self.assertEqual(player.played, [b"hello ", b"there."])

    def test_runtime_ignores_speech_until_wake_phrase(self):
        config = voice_channel_config({
            "api_key": "test-key",
            "wake": {
                "enabled": True,
                "wake_phrases": ["小智"],
            },
        })
        agent = FakeAgent()
        synth = FakeSynthesizer()
        player = FakePlayer()
        runtime = VoiceRuntime(
            agent=agent,
            config=config,
            microphone=FakeMicrophone(),
            recognizer=FakeRecognizer([
                VoiceUtterance(text="今天有人在聊天", language="zh"),
                VoiceUtterance(text="小智，几点了", language="zh"),
            ]),
            synthesizer=synth,
            player=player,
            options=VoiceRuntimeOptions(user_id="alice"),
            output=lambda *args, **kwargs: None,
        )

        asyncio.run(runtime.run_forever())

        self.assertEqual(agent.kwargs["user_message"], "几点了")
        self.assertEqual(len(synth.calls), 1)
        self.assertEqual(player.played, [b"hello ", b"there."])

    def test_runtime_phrase_only_wake_handles_next_utterance(self):
        config = voice_channel_config({
            "api_key": "test-key",
            "wake": {
                "enabled": True,
                "wake_phrases": ["小智"],
            },
        })
        agent = FakeAgent()
        runtime = VoiceRuntime(
            agent=agent,
            config=config,
            microphone=FakeMicrophone(),
            recognizer=FakeRecognizer([
                VoiceUtterance(text="小智", language="zh"),
                VoiceUtterance(text="明天提醒我喝水", language="zh"),
            ]),
            synthesizer=FakeSynthesizer(),
            player=FakePlayer(),
            options=VoiceRuntimeOptions(user_id="alice"),
            output=lambda *args, **kwargs: None,
        )

        asyncio.run(runtime.run_forever())

        self.assertEqual(agent.kwargs["user_message"], "明天提醒我喝水")

    def test_runtime_exit_phrase_returns_to_wake_waiting(self):
        config = voice_channel_config({
            "api_key": "test-key",
            "wake": {
                "enabled": True,
                "wake_phrases": ["小智"],
                "exit_phrases": ["退出"],
            },
        })
        agent = RecordingAgent()
        runtime = VoiceRuntime(
            agent=agent,
            config=config,
            microphone=FakeMicrophone(),
            recognizer=FakeRecognizer([
                VoiceUtterance(text="小智", language="zh"),
                VoiceUtterance(text="打开灯", language="zh"),
                VoiceUtterance(text="退出", language="zh"),
                VoiceUtterance(text="打开电视", language="zh"),
                VoiceUtterance(text="小智，打开空调", language="zh"),
            ]),
            synthesizer=FakeSynthesizer(),
            player=FakePlayer(),
            options=VoiceRuntimeOptions(user_id="alice"),
            output=lambda *args, **kwargs: None,
        )

        asyncio.run(runtime.run_forever())

        self.assertEqual(agent.messages, ["打开灯", "打开空调"])

    def test_runtime_sets_voice_delivery_context_for_scheduled_task_creation(self):
        config = voice_channel_config({"api_key": "test-key"})
        agent = ContextCapturingAgent()
        runtime = VoiceRuntime(
            agent=agent,
            config=config,
            microphone=FakeMicrophone(),
            recognizer=FakeRecognizer([VoiceUtterance(text="提醒我", language="zh")]),
            synthesizer=FakeSynthesizer(),
            player=FakePlayer(),
            options=VoiceRuntimeOptions(user_id="alice"),
            output=lambda *args, **kwargs: None,
        )

        asyncio.run(runtime.run_forever())

        self.assertEqual(agent.context.channel, "voice")
        self.assertEqual(agent.context.user_id, "alice")
        self.assertEqual(agent.context.target["user_id"], "alice")

    def test_runtime_dispatches_due_voice_message_task_to_tts(self):
        async def run_task():
            with tempfile.TemporaryDirectory() as tmpdir:
                enqueue_scheduled_task(
                    task_type="message",
                    content="该喝水了",
                    run_at=datetime.now() - timedelta(seconds=1),
                    tasks_dir=tmpdir,
                    channel="voice",
                    user_id="alice",
                    target={"user_id": "alice"},
                )
                synth = FakeSynthesizer()
                player = FakePlayer()
                runtime = VoiceRuntime(
                    agent=FakeAgent(),
                    config=voice_channel_config({"api_key": "test-key"}),
                    microphone=FakeMicrophone(),
                    recognizer=FakeRecognizer([]),
                    synthesizer=synth,
                    player=player,
                    options=VoiceRuntimeOptions(user_id="alice",tasks_dir=tmpdir),
                    output=lambda *args, **kwargs: None,
                )

                self.assertIsNotNone(runtime.task_scheduler)
                await runtime.task_scheduler.tick()

                self.assertEqual(synth.calls[0]["chunks"], ["该喝水了"])
                self.assertEqual(player.played, ["该喝水了".encode("utf-8")])
                self.assertEqual(list_active_task_records(tmpdir), [])

        asyncio.run(run_task())

    def test_runtime_dispatches_due_voice_agent_task_with_voice_context(self):
        async def run_task():
            with tempfile.TemporaryDirectory() as tmpdir:
                enqueue_scheduled_task(
                    task_type="agent",
                    content="查一下状态",
                    run_at=datetime.now() - timedelta(seconds=1),
                    tasks_dir=tmpdir,
                    channel="voice",
                    user_id="alice",
                    target={"user_id": "alice"},
                )
                agent = ScheduledAgent()
                synth = FakeSynthesizer()
                runtime = VoiceRuntime(
                    agent=agent,
                    config=voice_channel_config({"api_key": "test-key"}),
                    microphone=FakeMicrophone(),
                    recognizer=FakeRecognizer([]),
                    synthesizer=synth,
                    player=FakePlayer(),
                    options=VoiceRuntimeOptions(user_id="fallback",tasks_dir=tmpdir),
                    output=lambda *args, **kwargs: None,
                )

                self.assertIsNotNone(runtime.task_scheduler)
                await runtime.task_scheduler.tick()

                self.assertEqual(agent.kwargs["user_id"], "alice")
                self.assertIn("Scheduled task is due now", agent.kwargs["user_message"])
                self.assertEqual(agent.context.channel, "voice")
                self.assertEqual(agent.context.user_id, "alice")
                self.assertEqual(synth.calls[0]["chunks"], ["done"])
                self.assertEqual(list_active_task_records(tmpdir), [])

        asyncio.run(run_task())

    def test_runtime_default_keeps_microphone_paused_during_playback(self):
        config = voice_channel_config({"api_key": "test-key"})
        agent = FakeAgent()
        synth = FakeSynthesizer()
        runtime = VoiceRuntime(
            agent=agent,
            config=config,
            microphone=FakeMicrophone(),
            recognizer=FakeRecognizer([VoiceUtterance(text="你好", language="zh")]),
            synthesizer=synth,
            player=FakePlayer(),
            options=VoiceRuntimeOptions(user_id="alice"),
            output=lambda *args, **kwargs: None,
        )
        player = PauseAwarePlayer(runtime.pause_event)
        runtime.player = player

        asyncio.run(runtime.run_forever())

        self.assertTrue(player.pause_was_set)
        self.assertFalse(player.pause_was_cleared)

    def test_runtime_enabled_interruptions_clear_pause_during_playback(self):
        config = voice_channel_config({
            "api_key": "test-key",
            "enable_interruptions": True,
        })
        agent = FakeAgent()
        synth = FakeSynthesizer()
        runtime = VoiceRuntime(
            agent=agent,
            config=config,
            microphone=FakeMicrophone(),
            recognizer=FakeRecognizer([VoiceUtterance(text="你好", language="zh")]),
            synthesizer=synth,
            player=FakePlayer(),
            options=VoiceRuntimeOptions(user_id="alice"),
            output=lambda *args, **kwargs: None,
        )
        player = PauseAwarePlayer(runtime.pause_event, wait_for_clear=True)
        runtime.player = player

        asyncio.run(runtime.run_forever())

        self.assertTrue(player.pause_was_set)
        self.assertTrue(player.pause_was_cleared)

    def test_runtime_interrupt_cancels_current_reply_and_processes_new_utterance(self):
        config = voice_channel_config({
            "api_key": "test-key",
            "enable_interruptions": True,
        })
        agent = InterruptibleAgent()
        synth = FakeSynthesizer()
        player = FakePlayer()
        runtime = VoiceRuntime(
            agent=agent,
            config=config,
            microphone=FakeMicrophone(),
            recognizer=InterruptRecognizer(),
            synthesizer=synth,
            player=player,
            options=VoiceRuntimeOptions(user_id="alice"),
            output=lambda *args, **kwargs: None,
        )

        asyncio.run(runtime.run_forever())

        self.assertTrue(synth.cancelled)
        self.assertEqual(agent.messages, ["first", "interrupt"])

    def test_runtime_interrupt_stops_current_playback(self):
        config = voice_channel_config({
            "api_key": "test-key",
            "enable_interruptions": True,
        })
        agent = InterruptibleAgent()
        synth = StopAwareSynthesizer()
        player = StopAwarePlayer()
        runtime = VoiceRuntime(
            agent=agent,
            config=config,
            microphone=FakeMicrophone(),
            recognizer=InterruptRecognizer(),
            synthesizer=synth,
            player=player,
            options=VoiceRuntimeOptions(user_id="alice"),
            output=lambda *args, **kwargs: None,
        )

        asyncio.run(runtime.run_forever())

        self.assertTrue(synth.cancelled)
        self.assertTrue(synth.stop_event_was_set)
        self.assertTrue(player.stop_event_was_set)
        self.assertNotIn(b"should-not-play", player.played)

    def test_tts_batches_small_deltas_before_sending(self):
        chunks = list(_batch_text_chunks(["hel", "lo", " ", "there", "."], max_chars=80))

        self.assertEqual(chunks, ["hello there."])

    def test_tts_batches_on_max_chars(self):
        chunks = list(_batch_text_chunks(["abc", "def", "ghi"], max_chars=6))

        self.assertEqual(chunks, ["abcdef", "ghi"])

    def test_tts_timeout_loop_flushes_buffer_on_short_idle(self):
        tts = SonioxRealtimeTTS(api_key="test-key", config=VoiceTTSConfig(max_buffer_chars=80))
        ws = FakeWebSocket()
        items = iter(["hello", None])

        def next_item(timeout):
            del timeout
            try:
                return next(items)
            except StopIteration:
                raise StopIteration

        tts._send_timeout_aware_text_loop(
            ws,
            next_item,
            "stream-1",
            threading.Event(),
        )

        payloads = [json.loads(payload) for payload in ws.sent]
        self.assertEqual(payloads[0]["text"], "hello")
        self.assertFalse(payloads[0]["text_end"])
        self.assertEqual(payloads[-1]["text"], "")
        self.assertTrue(payloads[-1]["text_end"])

    def test_qwen_realtime_url_includes_model_query(self):
        url = _qwen_realtime_url(model="qwen3-tts-flash-realtime")

        self.assertEqual(
            url,
            "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen3-tts-flash-realtime",
        )

    def test_qwen_stt_session_update_uses_realtime_audio_config(self):
        config = voice_channel_config({"provider": "qwen", "api_key": "qwen-key"})
        stt = QwenRealtimeSTT(api_key="qwen-key", config=config.stt)

        event = stt._session_update_event()

        self.assertEqual(event["type"], "session.update")
        self.assertEqual(event["session"]["input_audio_format"], "pcm")
        self.assertEqual(event["session"]["sample_rate"], 16000)
        self.assertEqual(event["session"]["input_audio_transcription"]["language"], "zh")
        self.assertEqual(event["session"]["turn_detection"]["type"], "server_vad")
        self.assertEqual(event["session"]["turn_detection"]["threshold"], 0.2)
        self.assertEqual(event["session"]["turn_detection"]["silence_duration_ms"], 400)

    def test_qwen_stt_session_update_includes_session_options(self):
        config = voice_channel_config(
            {
                "provider": "qwen",
                "api_key": "qwen-key",
                "stt": {
                    "session_options": {
                        "custom_stt_option": True,
                    },
                },
            }
        )
        stt = QwenRealtimeSTT(api_key="qwen-key", config=config.stt)

        event = stt._session_update_event()

        self.assertTrue(event["session"]["custom_stt_option"])

    def test_qwen_stt_audio_loop_sends_base64_append_events(self):
        config = voice_channel_config({"provider": "qwen", "api_key": "qwen-key"})
        stt = QwenRealtimeSTT(api_key="qwen-key", config=config.stt)
        ws = FakeWebSocket()

        stt._send_audio_loop(
            ws,
            [b"audio"],
            threading.Lock(),
            threading.Event(),
            threading.Event(),
        )

        payload = json.loads(ws.sent[0])
        self.assertEqual(payload["type"], "input_audio_buffer.append")
        self.assertEqual(payload["audio"], "YXVkaW8=")

    def test_qwen_tts_session_update_uses_pcm_server_commit_defaults(self):
        config = voice_channel_config({"provider": "qwen", "api_key": "qwen-key"})
        tts = QwenRealtimeTTS(api_key="qwen-key", config=config.tts)

        event = tts._session_update_event(language="")

        self.assertEqual(event["type"], "session.update")
        self.assertEqual(event["session"]["mode"], "server_commit")
        self.assertEqual(event["session"]["voice"], "Cherry")
        self.assertEqual(event["session"]["language_type"], "Auto")
        self.assertEqual(event["session"]["response_format"], "pcm")
        self.assertEqual(event["session"]["sample_rate"], 24000)

    def test_qwen_tts_session_update_uses_detected_language_and_instructions(self):
        config = voice_channel_config(
            {
                "provider": "qwen",
                "api_key": "qwen-key",
                "tts": {
                    "instructions": "语速较快，适合介绍产品。",
                    "optimize_instructions": True,
                },
            }
        )
        tts = QwenRealtimeTTS(api_key="qwen-key", config=config.tts)

        event = tts._session_update_event(language="zh")

        self.assertEqual(event["session"]["language_type"], "Chinese")
        self.assertEqual(event["session"]["instructions"], "语速较快，适合介绍产品。")
        self.assertTrue(event["session"]["optimize_instructions"])

    def test_qwen_tts_session_update_includes_session_options(self):
        config = voice_channel_config(
            {
                "provider": "qwen",
                "api_key": "qwen-key",
                "tts": {
                    "session_options": {
                        "custom_tts_option": "value",
                    },
                },
            }
        )
        tts = QwenRealtimeTTS(api_key="qwen-key", config=config.tts)

        event = tts._session_update_event(language="")

        self.assertEqual(event["session"]["custom_tts_option"], "value")

    def test_qwen_tts_timeout_loop_flushes_text_and_finishes_session(self):
        config = voice_channel_config({"provider": "qwen", "api_key": "qwen-key"})
        tts = QwenRealtimeTTS(api_key="qwen-key", config=config.tts)
        ws = FakeWebSocket()
        items = iter(["hello", None])

        def next_item(timeout):
            del timeout
            try:
                return next(items)
            except StopIteration:
                raise StopIteration

        tts._send_timeout_aware_text_loop(ws, next_item, threading.Event())

        payloads = [json.loads(payload) for payload in ws.sent]
        self.assertEqual(payloads[0]["type"], "input_text_buffer.append")
        self.assertEqual(payloads[0]["text"], "hello")
        self.assertEqual(payloads[-1]["type"], "session.finish")

    def test_voice_factory_routes_qwen_provider_to_qwen_adapters(self):
        config = voice_channel_config({"provider": "qwen", "api_key": "qwen-key"})

        runtime = create_local_voice_runtime(
            agent=object(),
            config=config,
            options=VoiceRuntimeOptions(),
        )

        self.assertIsInstance(runtime.recognizer, QwenRealtimeSTT)
        self.assertIsInstance(runtime.synthesizer, QwenRealtimeTTS)

    def test_voice_factory_routes_custom_stt_tts_providers(self):
        config = voice_channel_config(
            {
                "provider": "custom",
                "stt": {"provider": "qwen", "api_key": "qwen-stt-key"},
                "tts": {"provider": "soniox", "api_key": "soniox-tts-key"},
            }
        )

        runtime = create_local_voice_runtime(
            agent=object(),
            config=config,
            options=VoiceRuntimeOptions(),
        )

        self.assertIsInstance(runtime.recognizer, QwenRealtimeSTT)
        self.assertIsInstance(runtime.synthesizer, SonioxRealtimeTTS)

    def test_voice_factory_requires_provider_selection(self):
        config = voice_channel_config({"api_key": "test-key"})

        with self.assertRaisesRegex(ValueError, "channels.voice.provider"):
            create_local_voice_runtime(
                agent=object(),
                config=config,
                options=VoiceRuntimeOptions(),
            )

    def test_qwen_connect_explains_missing_python_socks_for_socks_proxy(self):
        with patch(
            "websockets.sync.client.connect",
            side_effect=ImportError("python-socks is required to use a SOCKS proxy"),
        ):
            with self.assertRaisesRegex(QwenVoiceError, "python-socks"):
                _connect_qwen_websocket("wss://example.invalid/realtime", api_key="qwen-key")

    def test_soniox_connect_explains_missing_python_socks_for_socks_proxy(self):
        with patch(
            "websockets.sync.client.connect",
            side_effect=ImportError("python-socks is required to use a SOCKS proxy"),
        ):
            with self.assertRaisesRegex(SonioxVoiceError, "python-socks"):
                _connect_websocket("wss://example.invalid/realtime")


if __name__ == "__main__":
    unittest.main()
