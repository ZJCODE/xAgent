import asyncio
import unittest

from xagent.orchestrator import AgentOrchestrator, OrchestratorContext, TurnInput
from xagent.responses import TaskResult
from xagent.state import ConversationStateStore
from xagent.utils.tool_decorator import function_tool


class FakeResponsesEngine:
    def __init__(self):
        self.calls = []

    async def run(self, task, context, stream_callback=None):
        self.calls.append((task, context))
        if stream_callback is not None:
            maybe_awaitable = stream_callback("partial")
            if maybe_awaitable is not None:
                await maybe_awaitable
        return TaskResult(
            conversation_id=context.conversation_id,
            turn_id=context.turn_id,
            output=task.upper(),
            output_text=task.upper(),
        )


@function_tool(name="quick_reply", tier="realtime")
async def quick_reply(text: str) -> str:
    return f"quick:{text}"


class RuntimeSplitTests(unittest.IsolatedAsyncioTestCase):
    async def test_realtime_tool_stays_inside_orchestrator(self):
        state_store = ConversationStateStore()
        orchestrator = AgentOrchestrator(
            responses_engine=FakeResponsesEngine(),
            state_store=state_store,
            realtime_tools={"quick_reply": quick_reply},
        )

        result = await orchestrator.handle_turn(
            turn=TurnInput(text="ping", requested_tool="quick_reply"),
            context=OrchestratorContext(
                user_id="user-1",
                conversation_id="conv-1",
                turn_id="turn-1",
            ),
        )

        self.assertEqual(result.mode, "realtime_tool")
        self.assertEqual(result.output_text, "quick:ping")
        conversation = state_store.get_conversation("conv-1")
        self.assertIsNotNone(conversation)
        self.assertEqual([item.role for item in conversation.transcript], ["user", "assistant"])

    async def test_complex_turn_runs_as_background_job(self):
        state_store = ConversationStateStore()
        engine = FakeResponsesEngine()
        orchestrator = AgentOrchestrator(
            responses_engine=engine,
            state_store=state_store,
        )

        result = await orchestrator.handle_turn(
            turn=TurnInput(text="search the latest changelog"),
            context=OrchestratorContext(
                user_id="user-1",
                conversation_id="conv-2",
                turn_id="turn-2",
                allow_background=True,
            ),
        )

        self.assertEqual(result.mode, "background")
        self.assertIsNotNone(result.job_id)

        await asyncio.sleep(0.01)
        job = state_store.get_job(result.job_id)
        self.assertIsNotNone(job)
        self.assertEqual(job.status, "completed")
        self.assertEqual(len(engine.calls), 1)

    async def test_foreground_streaming_calls_stream_callback(self):
        state_store = ConversationStateStore()
        engine = FakeResponsesEngine()
        orchestrator = AgentOrchestrator(
            responses_engine=engine,
            state_store=state_store,
        )
        chunks = []

        async def on_stream(delta: str) -> None:
            chunks.append(delta)

        result = await orchestrator.handle_turn(
            turn=TurnInput(text="hello"),
            context=OrchestratorContext(
                user_id="user-1",
                conversation_id="conv-3",
                turn_id="turn-3",
                allow_background=False,
                stream=True,
            ),
            stream_callback=on_stream,
        )

        self.assertEqual(result.mode, "responses")
        self.assertEqual(chunks, ["partial"])
        self.assertEqual(result.output_text, "HELLO")
