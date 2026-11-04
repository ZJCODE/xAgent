import asyncio
import threading
import tempfile
import unittest
from unittest.mock import patch

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
from tests.test_agent_chat_flow import CapturingModelClient
from xagent.core.agent import Agent
from xagent.core.config import ReplyType
from xagent.schemas import RoleType


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




class PreemptiveRuntimeTests(unittest.TestCase):
    def test_default_partials_do_not_start_turns_and_final_speech_keeps_its_user(self):
        async def run_case():
            with tempfile.TemporaryDirectory() as root:
                agent = Agent(client=object(), workspace=root)
                model = CapturingModelClient([(ReplyType.SIMPLE_REPLY, "Hello Alice.")])
                agent.model_client = model
                runtime = VoiceRuntime(
                    agent=agent, config=voice_config(), microphone=FakeMicrophone(),
                    recognizer=FakeRecognizer([]), synthesizer=FakeSynthesizer(), player=FakePlayer(),
                    options=VoiceRuntimeOptions(user_id="alice"), output=lambda *a, **kw: None,
                )
                try:
                    for partial in ("please do this", "please do this later", "please do this later today"):
                        await runtime._preemptive.note_partial(partial)
                    self.assertEqual(model.calls, [])
                    self.assertEqual(await agent.message_storage.get_latest_message_cursor(), 0)
                    with patch("xagent.interfaces.voice.runtime._PLAYBACK_MICROPHONE_COOLDOWN_SECONDS", 0):
                        await runtime._reply_to_utterance(
                            VoiceUtterance("please do this later today"), endpoint_at=0, user_id="alice",
                        )
                    users = [message for message in await agent.message_storage.get_messages(10)
                             if message.role == RoleType.USER]
                    self.assertEqual([(message.sender_id, message.content) for message in users],
                                     [("alice", "please do this later today")])
                    self.assertEqual(len(model.calls), 1)
                finally:
                    await agent.working_context_compactor.cancel_refresh()
                    task = agent.memory_handler._maintenance_task
                    if task is not None:
                        await asyncio.gather(task, return_exceptions=True)

        asyncio.run(run_case())

    def test_adopted_partial_skips_second_chat(self):
        class CountingAgent(FakeAgent):
            def __init__(self):
                self.chat_calls = 0

            async def chat_events(self, **kwargs):
                self.chat_calls += 1
                async for event in super().chat_events(**kwargs):
                    yield event

        async def run_case():
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
