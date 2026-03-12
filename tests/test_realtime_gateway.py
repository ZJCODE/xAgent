import asyncio
import unittest

from xagent.orchestrator import AgentOrchestrator
from xagent.realtime import RealtimeClientEvent, RealtimeGateway
from xagent.responses import TaskResult
from xagent.state import ConversationStateStore
from xagent.utils.tool_decorator import function_tool


class FakeResponsesEngine:
    async def run(self, task, context, stream_callback=None):
        if stream_callback is not None:
            maybe_awaitable = stream_callback("he")
            if maybe_awaitable is not None:
                await maybe_awaitable
            maybe_awaitable = stream_callback("llo")
            if maybe_awaitable is not None:
                await maybe_awaitable
        return TaskResult(
            conversation_id=context.conversation_id,
            turn_id=context.turn_id,
            output=f"echo:{task}",
            output_text=f"echo:{task}",
        )


class FakeProvider:
    def __init__(self):
        self.sent_text = []
        self.audio_chunks = []
        self.commits = 0
        self.interrupts = 0
        self.closed = False

    async def connect(self, event_sink=None):
        self.event_sink = event_sink
        return True

    async def send_text(self, text: str):
        self.sent_text.append(text)
        return True

    async def append_audio_chunk(self, chunk: str):
        self.audio_chunks.append(chunk)
        return True

    async def commit_audio(self):
        self.commits += 1
        return True

    async def interrupt(self):
        self.interrupts += 1
        return True

    async def close(self):
        self.closed = True


class DisconnectedProvider(FakeProvider):
    async def connect(self, event_sink=None):
        self.event_sink = event_sink
        return False


@function_tool(name="flash_light", tier="realtime")
async def flash_light(text: str = "") -> str:
    return f"flash:{text}"


class RealtimeGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_turn_flows_through_gateway_and_clears_buffers(self):
        state_store = ConversationStateStore()
        gateway = RealtimeGateway(
            orchestrator=AgentOrchestrator(
                responses_engine=FakeResponsesEngine(),
                state_store=state_store,
            ),
            state_store=state_store,
            provider_cls=FakeProvider,
        )

        start_events = await gateway.handle_client_event(
            RealtimeClientEvent(type="session.start", user_id="user-1")
        )
        session_event = start_events[0]
        session_id = session_event.realtime_session_id
        conversation_id = session_event.conversation_id

        await gateway.handle_client_event(
            RealtimeClientEvent(
                type="input.text",
                realtime_session_id=session_id,
                conversation_id=conversation_id,
                payload={"text": "hello"},
            )
        )
        responses = await gateway.handle_client_event(
            RealtimeClientEvent(
                type="turn.commit",
                realtime_session_id=session_id,
                conversation_id=conversation_id,
                turn_id="turn-1",
                payload={},
            )
        )

        self.assertEqual([event.type for event in responses], ["ack", "turn.started", "turn.completed"])
        self.assertEqual(responses[-1].payload["output_text"], "echo:hello")
        session = state_store.get_live_session(session_id)
        self.assertEqual(session.buffered_text, "")
        self.assertEqual(session.active_turn_id, None)

    async def test_background_job_completes_via_session_queue(self):
        state_store = ConversationStateStore()
        gateway = RealtimeGateway(
            orchestrator=AgentOrchestrator(
                responses_engine=FakeResponsesEngine(),
                state_store=state_store,
            ),
            state_store=state_store,
            provider_cls=FakeProvider,
        )

        start_events = await gateway.handle_client_event(
            RealtimeClientEvent(type="session.start", user_id="user-1")
        )
        session_id = start_events[0].realtime_session_id
        conversation_id = start_events[0].conversation_id

        await gateway.handle_client_event(
            RealtimeClientEvent(
                type="input.text",
                realtime_session_id=session_id,
                conversation_id=conversation_id,
                payload={"text": "search release notes"},
            )
        )
        commit_events = await gateway.handle_client_event(
            RealtimeClientEvent(
                type="turn.commit",
                realtime_session_id=session_id,
                conversation_id=conversation_id,
                turn_id="turn-2",
            )
        )

        self.assertEqual(commit_events[-1].type, "job.started")
        queue = gateway.session_manager.get_event_queue(session_id)
        queued = await asyncio.wait_for(queue.get(), timeout=0.1)
        self.assertEqual(queued.type, "job.started")
        queued = await asyncio.wait_for(queue.get(), timeout=0.1)
        self.assertEqual(queued.type, "job.progress")
        queued = await asyncio.wait_for(queue.get(), timeout=0.1)
        self.assertEqual(queued.type, "job.completed")

    async def test_interrupt_marks_session_and_calls_provider(self):
        state_store = ConversationStateStore()
        gateway = RealtimeGateway(
            orchestrator=AgentOrchestrator(
                responses_engine=FakeResponsesEngine(),
                state_store=state_store,
            ),
            state_store=state_store,
            provider_cls=FakeProvider,
        )

        start_events = await gateway.handle_client_event(
            RealtimeClientEvent(type="session.start", user_id="user-1")
        )
        session_id = start_events[0].realtime_session_id
        conversation_id = start_events[0].conversation_id

        await gateway.handle_client_event(
            RealtimeClientEvent(
                type="interrupt",
                realtime_session_id=session_id,
                conversation_id=conversation_id,
            )
        )

        session = state_store.get_live_session(session_id)
        self.assertTrue(session.interrupted)

    async def test_gateway_realtime_tool_hint_bypasses_responses_engine(self):
        state_store = ConversationStateStore()
        gateway = RealtimeGateway(
            orchestrator=AgentOrchestrator(
                responses_engine=FakeResponsesEngine(),
                state_store=state_store,
                realtime_tools={"flash_light": flash_light},
            ),
            state_store=state_store,
            provider_cls=FakeProvider,
        )

        start_events = await gateway.handle_client_event(
            RealtimeClientEvent(type="session.start", user_id="user-1")
        )
        session_id = start_events[0].realtime_session_id
        conversation_id = start_events[0].conversation_id

        await gateway.handle_client_event(
            RealtimeClientEvent(
                type="input.text",
                realtime_session_id=session_id,
                conversation_id=conversation_id,
                payload={"text": "flash the room light"},
            )
        )
        responses = await gateway.handle_client_event(
            RealtimeClientEvent(
                type="turn.commit",
                realtime_session_id=session_id,
                conversation_id=conversation_id,
                turn_id="turn-3",
                payload={"tool_name": "flash_light"},
            )
        )

        self.assertEqual(responses[-1].type, "turn.completed")
        self.assertEqual(responses[-1].payload["output_text"], "flash:flash the room light")

    async def test_audio_only_turn_returns_explicit_error_without_provider_connection(self):
        state_store = ConversationStateStore()
        gateway = RealtimeGateway(
            orchestrator=AgentOrchestrator(
                responses_engine=FakeResponsesEngine(),
                state_store=state_store,
            ),
            state_store=state_store,
            provider_cls=DisconnectedProvider,
        )

        start_events = await gateway.handle_client_event(
            RealtimeClientEvent(type="session.start", user_id="user-1")
        )
        session_id = start_events[0].realtime_session_id
        conversation_id = start_events[0].conversation_id

        await gateway.handle_client_event(
            RealtimeClientEvent(
                type="input.audio.chunk",
                realtime_session_id=session_id,
                conversation_id=conversation_id,
                payload={"audio": "AAAA"},
            )
        )
        responses = await gateway.handle_client_event(
            RealtimeClientEvent(
                type="turn.commit",
                realtime_session_id=session_id,
                conversation_id=conversation_id,
                turn_id="turn-audio-1",
            )
        )

        self.assertEqual(responses[-1].type, "turn.completed")
        self.assertIn("Audio input requires", responses[-1].payload["error"])
