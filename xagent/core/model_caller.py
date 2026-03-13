"""
ModelCaller — encapsulates OpenAI API calls and retry logic.

Responsibilities:
- Making model API calls (structured output, streaming, plain)
- Retry with exponential back-off
- Stream event classification and text extraction
- Input message sanitisation
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Callable, List, Optional

from openai import AsyncOpenAI
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential

from ..defaults import RETRY_ATTEMPTS, RETRY_MIN_WAIT, RETRY_MAX_WAIT
from ..observability import observe
from ..schemas import MessageType


class ReplyType(Enum):
    """Types of replies the agent can generate."""

    SIMPLE_REPLY = "simple_reply"
    STRUCTURED_REPLY = "structured_reply"
    TOOL_CALL = "tool_call"
    ERROR = "error"


class ModelCaller:
    """Encapsulates OpenAI API calls and retry logic."""

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.client = client
        self.model = model
        self.logger = logger or logging.getLogger(__name__)

    @observe()
    @retry(
        stop=stop_after_attempt(RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=RETRY_MIN_WAIT, max=RETRY_MAX_WAIT),
    )
    async def call(
        self,
        input_msgs: list,
        system_prompt: str,
        tool_specs: Optional[list],
        output_type: Optional[type[BaseModel]] = None,
        stream: bool = False,
        store_reply_fn: Optional[Callable] = None,
        store_shared_reply_fn: Optional[Callable] = None,
    ) -> tuple:
        """
        Call the AI model and return ``(ReplyType, response)``.

        Args:
            input_msgs:  Conversation history as OpenAI-format dicts.
            system_prompt: Fully-built system prompt string.
            tool_specs: List of OpenAI tool spec dicts (may be None/empty).
            output_type: Pydantic model for structured-output calls.
            stream: Whether to stream the response.
            store_reply_fn: Async callable ``(text) -> None`` invoked after
                the full streaming text is assembled, to persist to history.
            store_shared_reply_fn: Same as above but for the shared session.

        Returns:
            ``(ReplyType, response_object)``
        """
        system_msg = {"role": "system", "content": system_prompt}
        messages = [system_msg] + self._sanitize_input_messages(input_msgs)

        try:
            if output_type is not None:
                response = await self.client.responses.parse(
                    model=self.model,
                    tools=tool_specs if tool_specs else [],
                    input=messages,
                    text_format=output_type,
                )
                if (
                    hasattr(response, "output_parsed")
                    and response.output_parsed is not None
                ):
                    return ReplyType.STRUCTURED_REPLY, response.output_parsed
            else:
                response = await self.client.responses.create(
                    model=self.model,
                    tools=tool_specs or [],
                    input=messages,
                    stream=stream,
                )

            if not stream:
                if hasattr(response, "output_text") and response.output_text:
                    return ReplyType.SIMPLE_REPLY, response.output_text
                if hasattr(response, "output") and response.output:
                    return ReplyType.TOOL_CALL, response.output
                self.logger.warning("Model response has no valid output: %s", response)
                return ReplyType.ERROR, "No valid output from model response."

            return await self._handle_stream(
                response, store_reply_fn, store_shared_reply_fn
            )

        except Exception as e:
            self.logger.exception("Model call failed: %s", e)
            if stream:
                msg = f"Model call error: {e}"

                async def _err_gen():
                    yield msg

                return ReplyType.ERROR, _err_gen()
            return ReplyType.ERROR, f"Model call error: {e}"

    # ------------------------------------------------------------------
    # Streaming helpers
    # ------------------------------------------------------------------

    async def _handle_stream(
        self,
        response,
        store_reply_fn: Optional[Callable],
        store_shared_reply_fn: Optional[Callable],
    ) -> tuple:
        """Classify a streaming response and return an appropriate generator."""
        prefix_events: list = []
        last_response = None
        stream_kind = None

        async for event in response:
            prefix_events.append(event)
            r = getattr(event, "response", None)
            if r is not None:
                last_response = r
            stream_kind = self._classify_stream_event(event)
            if stream_kind is not None:
                break

        if stream_kind == ReplyType.SIMPLE_REPLY:
            return ReplyType.SIMPLE_REPLY, self._make_text_stream(
                response, prefix_events, last_response,
                store_reply_fn, store_shared_reply_fn,
            )

        if stream_kind == ReplyType.TOOL_CALL:
            async for event in response:
                r = getattr(event, "response", None)
                if r is not None:
                    last_response = r
            tool_output = getattr(last_response, "output", None) or []
            if tool_output:
                return ReplyType.TOOL_CALL, tool_output
            self.logger.warning("Stream response ended without tool output")
            return ReplyType.ERROR, "No tool output from model response."

        # Fallback: try to extract text from what was already received
        final_text = self._extract_response_text(last_response)
        if final_text:
            async def _single():
                yield final_text

            return ReplyType.SIMPLE_REPLY, _single()

        self.logger.warning("Stream response contains no recognised output")

        async def _no_output():
            yield "No valid output from model response."

        return ReplyType.ERROR, _no_output()

    def _make_text_stream(
        self,
        response,
        prefix_events: list,
        initial_last_response,
        store_reply_fn: Optional[Callable],
        store_shared_reply_fn: Optional[Callable],
    ):
        """Return an async generator that yields text deltas and stores on finish."""

        async def _gen():
            last_resp = initial_last_response
            text_parts: List[str] = []

            for event in prefix_events:
                r = getattr(event, "response", None)
                if r is not None:
                    last_resp = r
                chunk = self._extract_stream_text_delta(event)
                if chunk:
                    text_parts.append(chunk)
                    yield chunk

            async for event in response:
                r = getattr(event, "response", None)
                if r is not None:
                    last_resp = r
                chunk = self._extract_stream_text_delta(event)
                if chunk:
                    text_parts.append(chunk)
                    yield chunk

            final_text = self._extract_response_text(last_resp, "".join(text_parts))
            if final_text and not text_parts:
                yield final_text
            if final_text:
                if store_reply_fn:
                    await store_reply_fn(final_text)
                if store_shared_reply_fn:
                    await store_shared_reply_fn(final_text)

        return _gen()

    # ------------------------------------------------------------------
    # Static helpers (reusable without an instance)
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize_input_messages(input_messages: list) -> list:
        """Remove leading FUNCTION_CALL_OUTPUT messages from the history."""
        while (
            input_messages
            and input_messages[0].get("type") == MessageType.FUNCTION_CALL_OUTPUT
        ):
            input_messages.pop(0)
        return input_messages

    @staticmethod
    def _classify_stream_event(event) -> Optional[ReplyType]:
        """Infer the high-level response kind from a single stream event."""
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
        """Return the text delta from a streaming event, or empty string."""
        if getattr(event, "type", None) == "response.output_text.delta":
            return getattr(event, "delta", "") or ""
        return ""

    @staticmethod
    def _extract_response_text(response, fallback: str = "") -> str:
        """Extract final text content from a completed model response object."""
        if response is None:
            return fallback
        output_text = getattr(response, "output_text", None)
        if output_text:
            return output_text
        for item in getattr(response, "output", None) or []:
            if getattr(item, "type", None) != "message":
                continue
            for content in getattr(item, "content", []) or []:
                text = getattr(content, "text", None)
                if text:
                    return text
        return fallback
