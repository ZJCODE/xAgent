import asyncio
import json
import threading
import unittest

from xagent.voice.config import VoiceChannelConfig, VoiceTTSConfig
from xagent.voice.factory import create_local_voice_runtime
from xagent.voice.qwen import QwenRealtimeSTT, QwenRealtimeTTS, _qwen_realtime_url
from xagent.voice.runtime import VoiceRuntime, VoiceRuntimeOptions, VoiceUtterance
from xagent.voice.soniox import SonioxRealtimeTTS, _batch_text_chunks


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


class VoiceRuntimeTests(unittest.TestCase):
    def test_runtime_routes_soniox_endpoint_utterance_to_agent_and_tts(self):
        config = VoiceChannelConfig.from_dict({
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
            options=VoiceRuntimeOptions(user_id="alice", enable_memory=False),
            output=lambda *args, **kwargs: output_lines.append("".join(str(arg) for arg in args)),
        )

        asyncio.run(runtime.run_forever())

        self.assertEqual(agent.kwargs["user_id"], "alice")
        self.assertFalse(agent.kwargs["enable_memory"])
        self.assertEqual(agent.kwargs["user_message"], "你好")
        self.assertEqual(synth.calls[0]["language"], "zh")
        self.assertEqual(synth.calls[0]["chunks"], ["hello ", "there."])
        self.assertEqual(player.played, [b"hello ", b"there."])

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
            "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime?model=qwen3-tts-flash-realtime",
        )

    def test_qwen_stt_session_update_uses_realtime_audio_config(self):
        config = VoiceChannelConfig.from_dict({"provider": "qwen", "api_key": "qwen-key"})
        stt = QwenRealtimeSTT(api_key="qwen-key", config=config.stt)

        event = stt._session_update_event()

        self.assertEqual(event["type"], "session.update")
        self.assertEqual(event["session"]["input_audio_format"], "pcm")
        self.assertEqual(event["session"]["sample_rate"], 16000)
        self.assertEqual(event["session"]["input_audio_transcription"]["language"], "zh")
        self.assertEqual(event["session"]["turn_detection"]["type"], "server_vad")
        self.assertEqual(event["session"]["turn_detection"]["threshold"], 0.2)
        self.assertEqual(event["session"]["turn_detection"]["silence_duration_ms"], 400)

    def test_qwen_stt_audio_loop_sends_base64_append_events(self):
        config = VoiceChannelConfig.from_dict({"provider": "qwen", "api_key": "qwen-key"})
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
        config = VoiceChannelConfig.from_dict({"provider": "qwen", "api_key": "qwen-key"})
        tts = QwenRealtimeTTS(api_key="qwen-key", config=config.tts)

        event = tts._session_update_event(language="")

        self.assertEqual(event["type"], "session.update")
        self.assertEqual(event["session"]["mode"], "server_commit")
        self.assertEqual(event["session"]["voice"], "Cherry")
        self.assertEqual(event["session"]["language_type"], "Auto")
        self.assertEqual(event["session"]["response_format"], "pcm")
        self.assertEqual(event["session"]["sample_rate"], 24000)

    def test_qwen_tts_session_update_uses_detected_language_and_instructions(self):
        config = VoiceChannelConfig.from_dict(
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

    def test_qwen_tts_timeout_loop_flushes_text_and_finishes_session(self):
        config = VoiceChannelConfig.from_dict({"provider": "qwen", "api_key": "qwen-key"})
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
        config = VoiceChannelConfig.from_dict({"provider": "qwen", "api_key": "qwen-key"})

        runtime = create_local_voice_runtime(
            agent=object(),
            config=config,
            options=VoiceRuntimeOptions(),
        )

        self.assertIsInstance(runtime.recognizer, QwenRealtimeSTT)
        self.assertIsInstance(runtime.synthesizer, QwenRealtimeTTS)


if __name__ == "__main__":
    unittest.main()
