"""System orchestrator coordinating realtime and Responses work."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Optional

from ..responses import ResponsesEngine, TaskContext
from ..state import ConversationStateStore
from .jobs import JobEventCallback, JobManager
from .models import OrchestratorContext, OrchestratorResult, TurnInput
from .policy import ToolPolicy
from .router import IntentRouter


class AgentOrchestrator:
    """Routes turns to realtime tools, foreground Responses, or background jobs."""

    def __init__(
        self,
        responses_engine: ResponsesEngine,
        state_store: ConversationStateStore,
        realtime_tools: Optional[dict[str, Any]] = None,
        intent_router: Optional[IntentRouter] = None,
        tool_policy: Optional[ToolPolicy] = None,
        job_manager: Optional[JobManager] = None,
    ):
        self.responses_engine = responses_engine
        self.state_store = state_store
        self.realtime_tools = realtime_tools or {}
        self.intent_router = intent_router or IntentRouter()
        self.tool_policy = tool_policy or ToolPolicy()
        self.job_manager = job_manager or JobManager(state_store=state_store)

    async def handle_turn(
        self,
        turn: TurnInput,
        context: OrchestratorContext,
        event_callback: JobEventCallback = None,
        stream_callback: Optional[Callable[[str], Awaitable[None] | None]] = None,
    ) -> OrchestratorResult:
        record = self.state_store.get_or_create_conversation(
            user_id=context.user_id,
            conversation_id=context.conversation_id,
        )
        if turn.text:
            await self.state_store.append_transcript(record.conversation_id, "user", turn.text)

        mode = self.intent_router.route(
            turn=turn,
            context=context,
            available_tools=self.realtime_tools.values(),
        )

        if mode == "realtime_tool" and turn.requested_tool:
            tool = self.realtime_tools[turn.requested_tool]
            self.tool_policy.check(tool, context)
            result = await tool(turn.text or "", **turn.metadata)
            output_text = str(result)
            await self.state_store.append_transcript(record.conversation_id, "assistant", output_text)
            self.state_store.add_tool_summary(record.conversation_id, f"{turn.requested_tool}: {output_text[:120]}")
            return OrchestratorResult(
                mode="realtime_tool",
                conversation_id=record.conversation_id,
                turn_id=context.turn_id,
                output=result,
                output_text=output_text,
                tool_name=turn.requested_tool,
            )

        task_context = TaskContext(
            user_id=context.user_id,
            conversation_id=record.conversation_id,
            turn_id=context.turn_id,
            realtime_session_id=context.realtime_session_id,
            image_source=turn.image_source,
            history_count=context.history_count,
            max_iter=context.max_iter,
            max_concurrent_tools=context.max_concurrent_tools,
            enable_memory=context.enable_memory,
            stream=context.stream,
            metadata=context.metadata,
        )

        if mode == "background":
            async def runner() -> str:
                result = await self.responses_engine.run(
                    task=turn.text,
                    context=task_context,
                )
                await self.state_store.append_transcript(
                    record.conversation_id,
                    "assistant",
                    result.output_text,
                    metadata={"response_id": result.response_id},
                )
                self.state_store.add_task_summary(record.conversation_id, result.plan.summary if result.plan else turn.text[:120])
                return result.output_text

            job = await self.job_manager.start_job(
                conversation_id=record.conversation_id,
                turn_id=context.turn_id,
                runner=runner,
                on_event=event_callback,
                metadata={"source": "responses"},
            )
            return OrchestratorResult(
                mode="background",
                conversation_id=record.conversation_id,
                turn_id=context.turn_id,
                job_id=job.job_id,
                metadata={"status": job.status},
            )

        result = await self.responses_engine.run(
            task=turn.text,
            context=task_context,
            stream_callback=stream_callback,
        )
        await self.state_store.append_transcript(
            record.conversation_id,
            "assistant",
            result.output_text,
            metadata={"response_id": result.response_id},
        )
        self.state_store.add_task_summary(
            record.conversation_id,
            result.plan.summary if result.plan else turn.text[:120],
        )
        return OrchestratorResult(
            mode="responses",
            conversation_id=record.conversation_id,
            turn_id=context.turn_id,
            response_id=result.response_id,
            output=result.output,
            output_text=result.output_text,
            metadata=result.metadata,
        )
