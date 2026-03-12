"""Execution loop for Responses tasks."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from .models import TaskContext, TaskResult


StreamCallback = Optional[Callable[[str], Awaitable[None] | None]]


class ResponsesExecutor:
    """Adapter around the model-backed task executor."""

    def __init__(self, agent: Any):
        self.agent = agent

    async def execute(
        self,
        user_message: str,
        context: TaskContext,
        stream_callback: StreamCallback = None,
    ) -> TaskResult:
        if context.stream and stream_callback is not None:
            response = await self.agent.chat(
                user_message=user_message,
                user_id=context.user_id,
                session_id=context.conversation_id,
                image_source=context.image_source,
                history_count=context.history_count,
                max_iter=context.max_iter,
                max_concurrent_tools=context.max_concurrent_tools,
                enable_memory=context.enable_memory,
                stream=True,
            )
            chunks: list[str] = []
            if hasattr(response, "__aiter__"):
                async for chunk in response:
                    if not chunk:
                        continue
                    chunks.append(str(chunk))
                    maybe_awaitable = stream_callback(str(chunk))
                    if maybe_awaitable is not None:
                        await maybe_awaitable
                final_text = "".join(chunks)
                return TaskResult(
                    conversation_id=context.conversation_id,
                    turn_id=context.turn_id,
                    output=final_text,
                    output_text=final_text,
                )

        response = await self.agent.chat(
            user_message=user_message,
            user_id=context.user_id,
            session_id=context.conversation_id,
            image_source=context.image_source,
            history_count=context.history_count,
            max_iter=context.max_iter,
            max_concurrent_tools=context.max_concurrent_tools,
            enable_memory=context.enable_memory,
        )
        output_text = str(response)
        return TaskResult(
            conversation_id=context.conversation_id,
            turn_id=context.turn_id,
            output=response,
            output_text=output_text,
        )
