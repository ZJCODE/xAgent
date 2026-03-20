import logging
from typing import Callable, Awaitable, List, Optional

from openai import AsyncOpenAI
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import AgentConfig, ReplyType
from ...schemas import MessageType


logger = logging.getLogger(__name__)


class ModelClient:
    """Handles all interactions with the OpenAI model: structured, non-stream, and stream responses."""

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
    ):
        self.client = client
        self.model = model

    @retry(
        stop=stop_after_attempt(AgentConfig.RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=AgentConfig.RETRY_MIN_WAIT, max=AgentConfig.RETRY_MAX_WAIT)
    )
    async def call(
        self,
        messages: list,
        tool_specs: Optional[list],
        instructions: Optional[str] = None,
        output_type: Optional[type[BaseModel]] = None,
        stream: bool = False,
        store_reply: Optional[Callable[..., Awaitable]] = None,
    ) -> tuple[ReplyType, object]:
        """
        Call the AI model with prepared messages.

        Args:
            messages: Input message list (user/assistant/tool content only).
            tool_specs: Tool specifications for the model.
            instructions: Static behavioural instructions (system prompt).
            output_type: Pydantic model for structured output.
            stream: Whether to stream the response.
            store_reply: Async callback to store the final reply text.
        Returns:
            Tuple of (ReplyType, response_object).
        """
        try:
            if output_type is not None:
                return await self._handle_structured(messages, tool_specs, output_type, instructions)

            response = await self.client.responses.create(
                model=self.model,
                instructions=instructions,
                tools=tool_specs or [],
                input=messages,
                stream=stream,
                truncation="auto",
            )

            if not stream:
                return self._handle_non_stream(response)
            else:
                return await self._handle_stream(response, store_reply)

        except Exception as e:
            logger.exception("Model call failed: %s", e)
            if stream:
                error_message = f"Model call error: {str(e)}"

                async def stream_generator():
                    yield error_message

                return ReplyType.ERROR, stream_generator()
            return ReplyType.ERROR, f"Model call error: {str(e)}"

    async def _handle_structured(
        self,
        messages: list,
        tool_specs: Optional[list],
        output_type: type[BaseModel],
        instructions: Optional[str] = None,
    ) -> tuple[ReplyType, object]:
        """Handle structured output via responses.parse."""
        response = await self.client.responses.parse(
            model=self.model,
            instructions=instructions,
            tools=tool_specs if tool_specs else [],
            input=messages,
            text_format=output_type,
            truncation="auto",
        )
        if hasattr(response, "output_parsed") and response.output_parsed is not None:
            return ReplyType.STRUCTURED_REPLY, response.output_parsed

        # Fall through to standard response handling
        return self._handle_non_stream(response)

    @staticmethod
    def _handle_non_stream(response) -> tuple[ReplyType, object]:
        """Handle a non-streaming model response."""
        if hasattr(response, 'output_text') and response.output_text:
            return ReplyType.SIMPLE_REPLY, response.output_text

        if hasattr(response, 'output') and response.output:
            return ReplyType.TOOL_CALL, response.output

        logger.warning("Model response contains no valid output: %s", response)
        return ReplyType.ERROR, "No valid output from model response."

    async def _handle_stream(
        self,
        response,
        store_reply: Optional[Callable[..., Awaitable]] = None,
    ) -> tuple[ReplyType, object]:
        """Handle a streaming model response."""
        prefix_events = []
        last_response = None
        stream_kind = None

        async for event in response:
            prefix_events.append(event)
            event_response = getattr(event, "response", None)
            if event_response is not None:
                last_response = event_response
            stream_kind = self._classify_stream_event(event)
            if stream_kind is not None:
                break

        if stream_kind == ReplyType.SIMPLE_REPLY:
            async def stream_generator():
                nonlocal last_response
                text_parts: List[str] = []

                for evt in prefix_events:
                    evt_response = getattr(evt, "response", None)
                    if evt_response is not None:
                        last_response = evt_response
                    content = self._extract_stream_text_delta(evt)
                    if content:
                        text_parts.append(content)
                        yield content

                async for evt in response:
                    evt_response = getattr(evt, "response", None)
                    if evt_response is not None:
                        last_response = evt_response
                    content = self._extract_stream_text_delta(evt)
                    if content:
                        text_parts.append(content)
                        yield content

                final_text = self._extract_response_text(
                    last_response,
                    "".join(text_parts),
                )
                if final_text and not text_parts:
                    yield final_text
                if final_text:
                    if store_reply:
                        await store_reply(final_text)

            return ReplyType.SIMPLE_REPLY, stream_generator()

        if stream_kind == ReplyType.TOOL_CALL:
            async for event in response:
                event_response = getattr(event, "response", None)
                if event_response is not None:
                    last_response = event_response

            tool_output = getattr(last_response, "output", None) or []
            if tool_output:
                return ReplyType.TOOL_CALL, tool_output

            logger.warning("Stream response ended without tool output")
            return ReplyType.ERROR, "No tool output from model response."

        final_text = self._extract_response_text(last_response)
        if final_text:
            async def stream_generator():
                yield final_text

            return ReplyType.SIMPLE_REPLY, stream_generator()

        logger.warning("Stream response contains no recognized output")

        async def stream_generator():
            yield "No valid output from model response."

        return ReplyType.ERROR, stream_generator()

    # ---- Static helpers ----

    @staticmethod
    def _classify_stream_event(event) -> Optional[ReplyType]:
        """Infer the high-level response kind from a stream event."""
        event_type = getattr(event, "type", None)
        if event_type == "response.output_text.delta":
            return ReplyType.SIMPLE_REPLY
        if event_type and "function_call" in event_type:
            return ReplyType.TOOL_CALL

        item = getattr(event, "item", None)
        item_type = getattr(item, "type", None)
        if item_type == "message":
            return ReplyType.SIMPLE_REPLY
        if item_type == "function_call":
            return ReplyType.TOOL_CALL

        resp = getattr(event, "response", None)
        if resp is not None:
            if getattr(resp, "output_text", ""):
                return ReplyType.SIMPLE_REPLY
            output = getattr(resp, "output", None) or []
            if any(getattr(o, "type", None) == "function_call" for o in output):
                return ReplyType.TOOL_CALL

        return None

    @staticmethod
    def _extract_stream_text_delta(event) -> str:
        """Return a text delta from a stream event when present."""
        if getattr(event, "type", None) == "response.output_text.delta":
            return getattr(event, "delta", "") or ""
        return ""

    @staticmethod
    def _extract_response_text(response, fallback_text: str = "") -> str:
        """Extract the final text content from a completed model response."""
        if response is None:
            return fallback_text

        output_text = getattr(response, "output_text", None)
        if output_text:
            return output_text

        output_items = getattr(response, "output", None) or []
        for output_item in output_items:
            if getattr(output_item, "type", None) != "message":
                continue
            for content_item in getattr(output_item, "content", []) or []:
                text_value = getattr(content_item, "text", None)
                if text_value:
                    return text_value

        return fallback_text
