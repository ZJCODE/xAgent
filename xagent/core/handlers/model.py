import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from openai import AsyncOpenAI
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import AgentConfig, ReplyType


logger = logging.getLogger(__name__)


@dataclass
class ChatToolCall:
    """Provider-neutral function tool call used internally by the agent loop."""

    call_id: str
    name: str
    arguments: str
    type: str = "function"

    @classmethod
    def from_raw(cls, raw_tool_call: Any) -> "ChatToolCall":
        function = ModelClient._field(raw_tool_call, "function") or {}
        return cls(
            call_id=ModelClient._field(raw_tool_call, "id") or "",
            name=ModelClient._field(function, "name") or "",
            arguments=ModelClient._field(function, "arguments") or "{}",
            type=ModelClient._field(raw_tool_call, "type") or "function",
        )

    def to_chat_dict(self) -> dict:
        return {
            "id": self.call_id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": self.arguments or "{}",
            },
        }


class ModelClient:
    """Handles Chat Completions model calls, including tools, structured JSON, and streaming."""

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
                stream = False

            response = await self.client.chat.completions.create(
                **self._build_create_params(
                    messages=messages,
                    tool_specs=tool_specs,
                    instructions=instructions,
                    output_type=output_type,
                    stream=stream,
                )
            )

            if stream:
                return await self._handle_stream(response, store_reply)
            return self._handle_non_stream(response, output_type)

        except Exception as e:
            logger.exception("Model call failed: %s", e)
            if stream:
                error_message = f"Model call error: {str(e)}"

                async def stream_generator():
                    yield error_message

                return ReplyType.ERROR, stream_generator()
            return ReplyType.ERROR, f"Model call error: {str(e)}"

    def _build_create_params(
        self,
        messages: list,
        tool_specs: Optional[list],
        instructions: Optional[str],
        output_type: Optional[type[BaseModel]],
        stream: bool,
    ) -> dict:
        params = {
            "model": self.model,
            "messages": self._build_chat_messages(messages, instructions, output_type),
            "stream": stream,
        }
        if tool_specs:
            params["tools"] = tool_specs
            params["tool_choice"] = "auto"
        if output_type is not None:
            params["response_format"] = {"type": "json_object"}
        return params

    @staticmethod
    def _build_chat_messages(
        messages: list,
        instructions: Optional[str],
        output_type: Optional[type[BaseModel]] = None,
    ) -> list:
        chat_messages = []
        system_content = ModelClient._structured_instructions(instructions, output_type)
        if system_content:
            chat_messages.append({"role": "system", "content": system_content})
        chat_messages.extend(messages)
        return chat_messages

    @staticmethod
    def _structured_instructions(
        instructions: Optional[str],
        output_type: Optional[type[BaseModel]],
    ) -> str:
        if output_type is None:
            return instructions or ""

        schema = json.dumps(output_type.model_json_schema(), ensure_ascii=False)
        structured_prompt = (
            "Structured output: return only one valid JSON object that conforms to this JSON schema. "
            "Do not wrap the JSON in markdown and do not include any prose before or after it.\n\n"
            f"JSON schema:\n{schema}"
        )
        if not instructions:
            return structured_prompt
        return f"{instructions}\n\n{structured_prompt}"

    @staticmethod
    def _handle_non_stream(
        response,
        output_type: Optional[type[BaseModel]] = None,
    ) -> tuple[ReplyType, object]:
        """Handle a non-streaming model response."""
        tool_calls = ModelClient._extract_tool_calls(response)
        if tool_calls:
            return ReplyType.TOOL_CALL, tool_calls

        text = ModelClient._extract_response_text(response)
        if output_type is not None and text:
            try:
                return ReplyType.STRUCTURED_REPLY, output_type.model_validate_json(text)
            except Exception as exc:
                logger.exception("Structured output validation failed: %s", exc)
                return ReplyType.ERROR, f"Structured output validation failed: {str(exc)}"

        if text:
            return ReplyType.SIMPLE_REPLY, text

        logger.warning("Model response contains no valid output: %s", response)
        return ReplyType.ERROR, "No valid output from model response."

    async def _handle_stream(
        self,
        response,
        store_reply: Optional[Callable[..., Awaitable]] = None,
    ) -> tuple[ReplyType, object]:
        """Handle a streaming model response."""
        prefix_chunks = []
        tool_call_parts: dict[int, dict] = {}
        stream_kind = None

        async for chunk in response:
            prefix_chunks.append(chunk)
            self._merge_stream_tool_calls(tool_call_parts, chunk)
            if self._chunk_has_tool_calls(chunk):
                stream_kind = ReplyType.TOOL_CALL
            elif self._extract_stream_text_delta(chunk):
                stream_kind = ReplyType.SIMPLE_REPLY
            if stream_kind is not None:
                break

        if stream_kind == ReplyType.SIMPLE_REPLY:
            async def stream_generator():
                text_parts: list[str] = []

                for chunk in prefix_chunks:
                    content = self._extract_stream_text_delta(chunk)
                    if content:
                        text_parts.append(content)
                        yield content

                async for chunk in response:
                    content = self._extract_stream_text_delta(chunk)
                    if content:
                        text_parts.append(content)
                        yield content

                final_text = "".join(text_parts)
                if final_text:
                    if store_reply:
                        await store_reply(final_text)

            return ReplyType.SIMPLE_REPLY, stream_generator()

        if stream_kind == ReplyType.TOOL_CALL:
            async for chunk in response:
                self._merge_stream_tool_calls(tool_call_parts, chunk)

            tool_calls = self._finalize_stream_tool_calls(tool_call_parts)
            if tool_calls:
                return ReplyType.TOOL_CALL, tool_calls

            logger.warning("Stream response ended without tool output")
            return ReplyType.ERROR, "No tool output from model response."

        logger.warning("Stream response contains no recognized output")

        async def stream_generator():
            yield "No valid output from model response."

        return ReplyType.ERROR, stream_generator()

    # ---- Static helpers ----

    @staticmethod
    def _field(obj: Any, name: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)

    @staticmethod
    def _first_choice(response) -> Any:
        choices = ModelClient._field(response, "choices") or []
        return choices[0] if choices else None

    @staticmethod
    def _extract_tool_calls(response) -> list:
        """Return normalized function tool calls from a completed chat response."""
        choice = ModelClient._first_choice(response)
        if choice is None:
            return []
        message = ModelClient._field(choice, "message")
        raw_tool_calls = ModelClient._field(message, "tool_calls") or []
        return [
            ChatToolCall.from_raw(tool_call)
            for tool_call in raw_tool_calls
            if ModelClient._field(tool_call, "type") in (None, "function")
        ]

    @staticmethod
    def _chunk_choices(chunk) -> list:
        return ModelClient._field(chunk, "choices") or []

    @staticmethod
    def _extract_stream_text_delta(chunk) -> str:
        """Return a text delta from a Chat Completions chunk when present."""
        for choice in ModelClient._chunk_choices(chunk):
            delta = ModelClient._field(choice, "delta")
            content = ModelClient._field(delta, "content")
            if isinstance(content, str) and content:
                return content
        return ""

    @staticmethod
    def _extract_response_text(response, fallback_text: str = "") -> str:
        """Extract assistant message text from a completed chat response."""
        choice = ModelClient._first_choice(response)
        if choice is None:
            return fallback_text

        message = ModelClient._field(choice, "message")
        content = ModelClient._field(message, "content")
        if isinstance(content, str) and content:
            return content
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if ModelClient._field(item, "type") == "text":
                    text = ModelClient._field(item, "text")
                    if text:
                        text_parts.append(text)
            if text_parts:
                return "".join(text_parts)

        return fallback_text

    @staticmethod
    def _chunk_has_tool_calls(chunk) -> bool:
        for choice in ModelClient._chunk_choices(chunk):
            delta = ModelClient._field(choice, "delta")
            if ModelClient._field(delta, "tool_calls"):
                return True
        return False

    @staticmethod
    def _merge_stream_tool_calls(tool_call_parts: dict[int, dict], chunk) -> None:
        for choice in ModelClient._chunk_choices(chunk):
            delta = ModelClient._field(choice, "delta")
            for raw_tool_call in ModelClient._field(delta, "tool_calls") or []:
                index = ModelClient._field(raw_tool_call, "index")
                if index is None:
                    index = len(tool_call_parts)
                part = tool_call_parts.setdefault(
                    int(index),
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                call_id = ModelClient._field(raw_tool_call, "id")
                if call_id:
                    part["id"] = call_id
                call_type = ModelClient._field(raw_tool_call, "type")
                if call_type:
                    part["type"] = call_type

                function = ModelClient._field(raw_tool_call, "function") or {}
                name = ModelClient._field(function, "name")
                if name:
                    part["function"]["name"] = name
                arguments = ModelClient._field(function, "arguments")
                if arguments:
                    part["function"]["arguments"] += arguments

    @staticmethod
    def _finalize_stream_tool_calls(tool_call_parts: dict[int, dict]) -> list[ChatToolCall]:
        tool_calls = []
        for index in sorted(tool_call_parts):
            part = tool_call_parts[index]
            if not part["id"]:
                part["id"] = f"call_{index}"
            tool_call = ChatToolCall.from_raw(part)
            if tool_call.name:
                tool_calls.append(tool_call)
        return tool_calls
