import asyncio
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from xagent.interfaces.voice.attention import VoiceAttentionConfig, VoiceAttentionGate
from xagent.interfaces.voice.presence import SttLifecycleController, VoicePresenceConfig, pcm16_rms
from xagent.interfaces.voice.proactive import ProactiveSpeechLimiter, in_quiet_hours
from xagent.interfaces.voice.runtime import VoiceRuntime, VoiceRuntimeOptions
from xagent.interfaces.voice.speakers import SpeakerBindingStore
from xagent.interfaces.voice.types import VoiceUtterance
from tests.test_voice_runtime import (
    FakeAgent,
    FakeMicrophone,
    FakePlayer,
    FakeRecognizer,
    FakeSynthesizer,
    voice_config,
)


class AttentionTests(unittest.TestCase):
    def test_open_window_then_wake_term(self):
        gate = VoiceAttentionGate(
            config=VoiceAttentionConfig(open_window_seconds=30.0, wake_terms=["xBot"]),
            wake_terms=["xBot"],
        )
        with patch("xagent.interfaces.voice.attention.time.monotonic", return_value=100.0):
            gate._window_open_until = 130.0
            self.assertEqual(gate.attention_tier("hello"), 1)
        with patch("xagent.interfaces.voice.attention.time.monotonic", return_value=200.0):
            gate._window_open_until = 130.0
            self.assertEqual(gate.attention_tier("hey xBot"), 2)
            self.assertEqual(gate.attention_tier("tv noise"), 3)


class SpeakerBindingTests(unittest.TestCase):
    def test_persist_and_resolve(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bindings.json"
            store = SpeakerBindingStore(path)
            first = store.resolve(speaker_label="2", fallback_user_id="local_voice", confidence=0.8)
            self.assertEqual(first.user_id, "local_voice#2")
            self.assertFalse(first.bound)
            store.bind("2", "alice")
            second = store.resolve(speaker_label="2", fallback_user_id="local_voice")
            self.assertEqual(second.user_id, "alice")
            self.assertTrue(second.bound)


class PresenceTests(unittest.TestCase):
    def test_rms_and_idle_close(self):
        chunk = (b"\x00\x10" * 100)
        self.assertGreater(pcm16_rms(chunk), 0.0)
        cfg = VoicePresenceConfig(close_stt_after_idle_seconds=5.0, wake_energy_rms=10.0)
        lifecycle = SttLifecycleController(cfg)
        lifecycle.observe_audio(chunk)
        with patch("xagent.interfaces.voice.presence.time.monotonic", return_value=100.0):
            lifecycle._last_activity = 90.0
            self.assertTrue(lifecycle.should_close_session())
        lifecycle.note_endpoint()
        self.assertTrue(lifecycle.heard_recently())


class ProactivePolicyTests(unittest.TestCase):
    def test_quiet_hours_wraps_midnight(self):
        self.assertTrue(
            in_quiet_hours(quiet_start=22, quiet_end=7, now=datetime(2026, 1, 1, 23, 0))
        )
        self.assertFalse(
            in_quiet_hours(quiet_start=22, quiet_end=7, now=datetime(2026, 1, 1, 12, 0))
        )

    def test_rate_limiter(self):
        limiter = ProactiveSpeechLimiter(max_per_hour=2)
        self.assertTrue(limiter.allow())
        limiter.record()
        self.assertTrue(limiter.allow())
        limiter.record()
        self.assertFalse(limiter.allow())


class ObservingAgent(FakeAgent):
    async def observe(self, **kwargs):
        self.observed = kwargs
        return SimpleNamespace()


class VoiceAttentionRuntimeTests(unittest.TestCase):
    def test_tier_three_observes_without_chat(self):
        agent = ObservingAgent()
        config = voice_config()
        attention_gate = VoiceAttentionGate(
            config=VoiceAttentionConfig(
                open_window_seconds=0.0,
                wake_terms=[],
                use_decide_participation=False,
            )
        )
        runtime = VoiceRuntime(
            agent=agent,
            config=config,
            microphone=FakeMicrophone(),
            recognizer=FakeRecognizer([VoiceUtterance("background chatter")]),
            synthesizer=FakeSynthesizer(),
            player=FakePlayer(),
            options=VoiceRuntimeOptions(user_id="alice"),
            output=lambda *args, **kwargs: None,
            attention_gate=attention_gate,
        )
        asyncio.run(runtime.run_forever())
        self.assertFalse(hasattr(agent, "kwargs"))
        self.assertTrue(hasattr(agent, "observed"))

    def test_tier_one_dispatches(self):
        agent = FakeAgent()
        attention_gate = VoiceAttentionGate(
            config=VoiceAttentionConfig(open_window_seconds=60.0),
        )
        runtime = VoiceRuntime(
            agent=agent,
            config=voice_config(),
            microphone=FakeMicrophone(),
            recognizer=FakeRecognizer([VoiceUtterance("hello")]),
            synthesizer=FakeSynthesizer(),
            player=FakePlayer(),
            options=VoiceRuntimeOptions(user_id="alice"),
            output=lambda *args, **kwargs: None,
            attention_gate=attention_gate,
        )
        with patch("xagent.interfaces.voice.runtime._PLAYBACK_MICROPHONE_COOLDOWN_SECONDS", 0.0):
            asyncio.run(runtime.run_forever())
        self.assertEqual(agent.kwargs["room_name"], runtime.config.room_name)
        self.assertEqual(agent.kwargs["user_message"], "hello")
