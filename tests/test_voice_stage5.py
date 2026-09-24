import asyncio
import threading
import unittest
from unittest.mock import patch

from xagent.interfaces.voice.ack import DEFAULT_ACK_PHRASES, InstantAckConfig, InstantAckSpeaker
from xagent.interfaces.voice.audio import SoundDevicePlayer
from xagent.interfaces.voice.preemptive import (
    PreemptiveGenerationController,
    PreemptiveGenerationConfig,
    transcripts_compatible,
)
from xagent.interfaces.voice.runtime import VoiceRuntime, VoiceRuntimeOptions
from xagent.interfaces.voice.types import VoiceUtterance
from tests.test_voice_runtime import (
    FakeAgent,
    FakeMicrophone,
    FakePlayer,
    FakeRecognizer,
    FakeSynthesizer,
    voice_config,
)


class PreemptiveTests(unittest.TestCase):
    def test_transcript_match_is_exact(self):
        self.assertTrue(transcripts_compatible("hello there", "hello there"))
        self.assertFalse(transcripts_compatible("hello", "hello there"))

    def test_adopt_replays_buffered_events(self):
        async def run_case():
            async def chat_events(**kwargs):
                del kwargs
                yield {"type": "message_delta", "message_id": "1", "delta": "hi"}
                yield {"type": "message_done", "message_id": "1", "content": "hi"}

            controller = PreemptiveGenerationController(
                PreemptiveGenerationConfig(enabled=True, min_chars=3)
            )
            controller.configure(chat_events, stream=True)
            await controller.note_partial("ready")
            await asyncio.sleep(0.05)
            stream = await controller.adopt_events("ready")
            self.assertIsNotNone(stream)
            assert stream is not None
            events = [event async for event in stream]
            self.assertEqual(events[0]["type"], "message_delta")

        asyncio.run(run_case())


class InstantAckTests(unittest.TestCase):
    def test_default_ack_phrase_is_minimal(self):
        self.assertEqual(
            DEFAULT_ACK_PHRASES,
            ("[thoughtful]Hmm…", "[thoughtful]Mmm…", "[thoughtful]嗯..."),
        )

    def test_cooldown_blocks_back_to_back(self):
        speaker = InstantAckSpeaker(InstantAckConfig(cooldown_seconds=60.0))
        self.assertTrue(speaker.should_play())
        speaker.pick_phrase()
        self.assertFalse(speaker.should_play())


class WarmPlayerTests(unittest.TestCase):
    def test_close_is_safe_without_stream(self):
        player = SoundDevicePlayer(keep_warm=True)
        player.close()


class PreemptiveRuntimeTests(unittest.TestCase):
    def test_adopted_partial_skips_second_chat(self):
        class CountingAgent(FakeAgent):
            def __init__(self):
                self.chat_calls = 0

            async def chat_events(self, **kwargs):
                self.chat_calls += 1
                async for event in super().chat_events(**kwargs):
                    yield event

        async def run_case():
            from xagent.interfaces.voice.ack import InstantAckConfig, InstantAckSpeaker
            from xagent.interfaces.voice.preemptive import (
                PreemptiveGenerationConfig,
                PreemptiveGenerationController,
            )

            agent = CountingAgent()
            config = voice_config()
            runtime = VoiceRuntime(
                agent=agent,
                config=config,
                microphone=FakeMicrophone(),
                recognizer=FakeRecognizer([VoiceUtterance("hello")]),
                synthesizer=FakeSynthesizer(),
                player=FakePlayer(),
                options=VoiceRuntimeOptions(user_id="alice"),
                output=lambda *args, **kwargs: None,
            )
            runtime._instant_ack = InstantAckSpeaker(InstantAckConfig(enabled=False))
            abort_turn = getattr(agent, "abort", None)
            runtime._preemptive = PreemptiveGenerationController(
                PreemptiveGenerationConfig(enabled=True, min_chars=3),
                on_abort=abort_turn if callable(abort_turn) else None,
            )
            from xagent.interfaces.voice.speech_text import VOICE_CHANNEL_INSTRUCTIONS

            runtime._preemptive.configure(
                agent.chat_events,
                stream=True,
                channel="voice",
                inbox_kind="user_turn",
                channel_instructions=VOICE_CHANNEL_INSTRUCTIONS,
                room_name=config.room_name,
                max_agent_loops=config.performance.max_agent_loops,
            )
            await runtime._preemptive.note_partial("hello")
            await asyncio.sleep(0.05)
            with patch("xagent.interfaces.voice.runtime._PLAYBACK_MICROPHONE_COOLDOWN_SECONDS", 0.0):
                await runtime._reply_to_utterance(
                    VoiceUtterance("hello"),
                    endpoint_at=0.0,
                )
            self.assertEqual(agent.chat_calls, 1)

        asyncio.run(run_case())
