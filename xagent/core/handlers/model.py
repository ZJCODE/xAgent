import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Union

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
    reasoning_content: Optional[str] = None
    content_blocks: Optional[list[dict]] = None
    type: str = "function"

    @classmethod
    def from_raw(
        cls,
        raw_tool_call: Any,
        reasoning_content: Optional[str] = None,
    ) -> "ChatToolCall":
        function = ModelClient._field(raw_tool_call, "function") or {}
        return cls(
            call_id=ModelClient._field(raw_tool_call, "id") or "",
            name=ModelClient._field(function, "name") or "",
            arguments=ModelClient._field(function, "arguments") or "{}",
            reasoning_content=reasoning_content,
            type=ModelClient._field(raw_tool_call, "type") or "function",
        )

    @classmethod
    def from_anthropic_block(
        cls,
        raw_tool_block: Any,
        content_blocks: Optional[list[dict]] = None,
    ) -> "ChatToolCall":
        input_value = ModelClient._field(raw_tool_block, "input") or {}
        return cls(
            call_id=ModelClient._field(raw_tool_block, "id") or "",
            name=ModelClient._field(raw_tool_block, "name") or "",
            arguments=ModelClient._json_dumps(input_value),
            content_blocks=content_blocks,
            type="function",
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


@dataclass(frozen=True)
class ModelErrorEvent:
    """Provider-neutral model error payload for internal handling."""

    code: str
    message: str
    details: Optional[str] = None


class ModelClient:
    """Handles model calls across OpenAI Chat Completions and Anthropic Messages."""

    def __init__(
        self,
        client: Any,
        model: str,
        backend: str = "openai",
        max_tokens: int = AgentConfig.DEFAULT_MAX_TOKENS,
    ):
        self.client = client
        self.model = model
        self.backend = self._normalize_backend(backend)
        self.max_tokens = max_tokens

    @retry(
        stop=stop_after_attempt(AgentConfig.RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=AgentConfig.RETRY_MIN_WAIT, max=AgentConfig.RETRY_MAX_WAIT)
    )
    async def call(
        self,
        messages: list,
        tool_specs: Optional[list],
        instructions: Optional[Union[str, list[dict]]] = None,
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

            if self.backend == "anthropic":
                response = await self.client.messages.create(
                    **self._build_anthropic_create_params(
                        messages=messages,
                        tool_specs=tool_specs,
                        instructions=instructions,
                        output_type=output_type,
                        stream=stream,
                    )
                )
                if stream:
                    return await self._handle_anthropic_stream(response, store_reply)
                return self._handle_anthropic_non_stream(response, output_type)

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
            return ReplyType.ERROR, ModelErrorEvent(
                code="model_call_failed",
                message="Model call failed.",
                details=str(e),
            )

    def _build_create_params(
        self,
        messages: list,
        tool_specs: Optional[list],
        instructions: Optional[Union[str, list[dict]]],
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

    def _build_anthropic_create_params(
        self,
        messages: list,
        tool_specs: Optional[list],
        instructions: Optional[Union[str, list[dict]]],
        output_type: Optional[type[BaseModel]],
        stream: bool,
    ) -> dict:
        system, anthropic_messages = self._build_anthropic_messages(
            messages=messages,
            instructions=instructions,
            output_type=output_type,
        )
        params = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": anthropic_messages,
            "stream": stream,
        }
        if system:
            params["system"] = system
        if tool_specs:
            params["tools"] = self._to_anthropic_tools(tool_specs)
            params["tool_choice"] = {"type": "auto"}
        return params

    @staticmethod
    def _build_chat_messages(
        messages: list,
        instructions: Optional[Union[str, list[dict]]],
        output_type: Optional[type[BaseModel]] = None,
        strip_provider_extras: bool = True,
    ) -> list:
        chat_messages = []
        if isinstance(instructions, list):
            chat_messages.extend(dict(message) for message in instructions)
            structured_content = ModelClient._structured_instructions(None, output_type)
            if structured_content:
                chat_messages.append({
                    "role": "system",
                    "name": "structured_output",
                    "content": structured_content,
                })
            chat_messages.extend(messages)
            return ModelClient._strip_message_names(
                chat_messages,
                strip_provider_extras=strip_provider_extras,
            )

        system_content = ModelClient._structured_instructions(instructions, output_type)
        if system_content:
            chat_messages.append({"role": "system", "content": system_content})
        chat_messages.extend(messages)
        return ModelClient._strip_message_names(
            chat_messages,
            strip_provider_extras=strip_provider_extras,
        )

    @staticmethod
    def _strip_message_names(messages: list, *, strip_provider_extras: bool = True) -> list:
        """Remove top-level Chat message names for provider compatibility."""
        stripped_messages = []
        for message in messages:
            if not isinstance(message, dict):
                stripped_messages.append(message)
                continue
            sanitized = dict(message)
            sanitized.pop("name", None)
            if strip_provider_extras:
                sanitized.pop("content_blocks", None)
            stripped_messages.append(sanitized)
        return stripped_messages

    @classmethod
    def _build_anthropic_messages(
        cls,
        messages: list,
        instructions: Optional[Union[str, list[dict]]],
        output_type: Optional[type[BaseModel]] = None,
    ) -> tuple[str, list[dict]]:
        chat_messages = cls._build_chat_messages(
            messages,
            instructions,
            output_type,
            strip_provider_extras=False,
        )
        system_parts: list[str] = []
        anthropic_messages: list[dict] = []

        for message in chat_messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "").strip()
            content = message.get("content")
            if role == "system":
                content_text = cls._content_to_text(content)
                if content_text:
                    system_parts.append(content_text)
                continue
            converted = cls._to_anthropic_message(message)
            if converted is not None:
                anthropic_messages.append(converted)

        return "\n\n".join(system_parts), cls._coalesce_anthropic_messages(anthropic_messages)

    @classmethod
    def _to_anthropic_message(cls, message: dict) -> Optional[dict]:
        role = str(message.get("role") or "").strip()
        if role not in {"user", "assistant", "tool"}:
            return None

        if role == "tool":
            return {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": str(message.get("tool_call_id") or "call_0"),
                    "content": cls._content_to_text(message.get("content")),
                }],
            }

        content_blocks = message.get("content_blocks")
        if role == "assistant" and isinstance(content_blocks, list):
            return {
                "role": "assistant",
                "content": cls._normalize_anthropic_content_blocks(content_blocks),
            }

        tool_calls = message.get("tool_calls") or []
        if role == "assistant" and tool_calls:
            blocks = []
            content_text = cls._content_to_text(message.get("content"))
            if content_text:
                blocks.append({"type": "text", "text": content_text})
            reasoning_content = message.get("reasoning_content")
            if isinstance(reasoning_content, str) and reasoning_content:
                blocks.append({"type": "thinking", "thinking": reasoning_content})
            for tool_call in tool_calls:
                function = cls._field(tool_call, "function") or {}
                blocks.append({
                    "type": "tool_use",
                    "id": cls._field(tool_call, "id") or "call_0",
                    "name": cls._field(function, "name") or "",
                    "input": cls._json_loads(cls._field(function, "arguments") or "{}"),
                })
            return {"role": "assistant", "content": blocks}

        return {
            "role": role,
            "content": cls._to_anthropic_content(message.get("content")),
        }

    @classmethod
    def _to_anthropic_content(cls, content: Any) -> Union[str, list[dict]]:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return cls._content_to_text(content)

        blocks: list[dict] = []
        for item in content:
            item_type = cls._field(item, "type")
            if item_type == "text":
                text = cls._field(item, "text")
                if text:
                    blocks.append({"type": "text", "text": str(text)})
                continue
            if item_type == "image_url":
                image_url = cls._field(item, "image_url") or {}
                url = cls._field(image_url, "url")
                image_block = cls._to_anthropic_image_block(url)
                if image_block is not None:
                    blocks.append(image_block)
                continue
            text = cls._content_to_text(item)
            if text:
                blocks.append({"type": "text", "text": text})
        return blocks or ""

    @staticmethod
    def _to_anthropic_image_block(url: Any) -> Optional[dict]:
        if not isinstance(url, str) or not url:
            return None
        if url.startswith("data:image/") and ";base64," in url:
            media_type, data = url[5:].split(";base64,", 1)
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": data,
                },
            }
        if url.startswith(("http://", "https://")):
            return {
                "type": "image",
                "source": {
                    "type": "url",
                    "url": url,
                },
            }
        return None

    @classmethod
    def _coalesce_anthropic_messages(cls, messages: list[dict]) -> list[dict]:
        coalesced: list[dict] = []
        for message in messages:
            if coalesced and coalesced[-1]["role"] == message["role"]:
                coalesced[-1]["content"] = cls._merge_anthropic_content(
                    coalesced[-1]["content"],
                    message["content"],
                )
                continue
            coalesced.append(message)
        return coalesced

    @classmethod
    def _merge_anthropic_content(cls, left: Any, right: Any) -> Union[str, list[dict]]:
        if isinstance(left, str) and isinstance(right, str):
            if not left:
                return right
            if not right:
                return left
            return f"{left}\n\n{right}"
        return [*cls._as_anthropic_blocks(left), *cls._as_anthropic_blocks(right)]

    @staticmethod
    def _as_anthropic_blocks(content: Any) -> list[dict]:
        if isinstance(content, list):
            return content
        if isinstance(content, str) and content:
            return [{"type": "text", "text": content}]
        return []

    @classmethod
    def _to_anthropic_tools(cls, tool_specs: list) -> list[dict]:
        tools = []
        for tool_spec in tool_specs:
            function = cls._field(tool_spec, "function") or tool_spec
            name = cls._field(function, "name")
            if not name:
                continue
            tools.append({
                "name": name,
                "description": cls._field(function, "description") or "",
                "input_schema": cls._field(function, "parameters") or {"type": "object"},
            })
        return tools

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
                return ReplyType.ERROR, ModelErrorEvent(
                    code="structured_output_validation_failed",
                    message="Structured output validation failed.",
                    details=str(exc),
                )

        if text:
            return ReplyType.SIMPLE_REPLY, text

        logger.warning("Model response contains no valid output: %s", response)
        return ReplyType.ERROR, ModelErrorEvent(
            code="empty_model_response",
            message="No valid output from model response.",
        )

    @staticmethod
    def _handle_anthropic_non_stream(
        response,
        output_type: Optional[type[BaseModel]] = None,
    ) -> tuple[ReplyType, object]:
        """Handle a non-streaming Anthropic Messages response."""
        tool_calls = ModelClient._extract_anthropic_tool_calls(response)
        if tool_calls:
            return ReplyType.TOOL_CALL, tool_calls

        text = ModelClient._extract_anthropic_response_text(response)
        if output_type is not None and text:
            try:
                return ReplyType.STRUCTURED_REPLY, output_type.model_validate_json(text)
            except Exception as exc:
                logger.exception("Structured output validation failed: %s", exc)
                return ReplyType.ERROR, ModelErrorEvent(
                    code="structured_output_validation_failed",
                    message="Structured output validation failed.",
                    details=str(exc),
                )

        if text:
            return ReplyType.SIMPLE_REPLY, text

        logger.warning("Anthropic response contains no valid output: %s", response)
        return ReplyType.ERROR, ModelErrorEvent(
            code="empty_model_response",
            message="No valid output from model response.",
        )

    async def _handle_stream(
        self,
        response,
        store_reply: Optional[Callable[..., Awaitable]] = None,
    ) -> tuple[ReplyType, object]:
        """Handle a streaming model response."""
        prefix_chunks = []
        reasoning_parts: list[str] = []
        tool_call_parts: dict[int, dict] = {}
        stream_kind = None

        async for chunk in response:
            prefix_chunks.append(chunk)
            reasoning_delta = self._extract_stream_reasoning_delta(chunk)
            if reasoning_delta:
                reasoning_parts.append(reasoning_delta)
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
                reasoning_delta = self._extract_stream_reasoning_delta(chunk)
                if reasoning_delta:
                    reasoning_parts.append(reasoning_delta)
                self._merge_stream_tool_calls(tool_call_parts, chunk)

            reasoning_content = "".join(reasoning_parts) if reasoning_parts else None
            tool_calls = self._finalize_stream_tool_calls(tool_call_parts, reasoning_content)
            if tool_calls:
                return ReplyType.TOOL_CALL, tool_calls

            logger.warning("Stream response ended without tool output")
            return ReplyType.ERROR, ModelErrorEvent(
                code="empty_tool_response",
                message="No tool output from model response.",
            )

        logger.warning("Stream response contains no recognized output")
        return ReplyType.ERROR, ModelErrorEvent(
            code="empty_stream_response",
            message="No valid output from model response.",
        )

    async def _handle_anthropic_stream(
        self,
        response,
        store_reply: Optional[Callable[..., Awaitable]] = None,
    ) -> tuple[ReplyType, object]:
        """Handle a streaming Anthropic Messages response."""
        prefix_events = []
        content_blocks: dict[int, dict] = {}
        stream_kind = None

        async for event in response:
            prefix_events.append(event)
            self._merge_anthropic_stream_event(content_blocks, event)
            if self._anthropic_event_starts_tool_use(event):
                stream_kind = ReplyType.TOOL_CALL
                break
            if self._extract_anthropic_stream_text_delta(event):
                stream_kind = ReplyType.SIMPLE_REPLY
                break

        if stream_kind == ReplyType.SIMPLE_REPLY:
            async def stream_generator():
                text_parts: list[str] = []

                for event in prefix_events:
                    content = self._extract_anthropic_stream_text_delta(event)
                    if content:
                        text_parts.append(content)
                        yield content

                async for event in response:
                    content = self._extract_anthropic_stream_text_delta(event)
                    if content:
                        text_parts.append(content)
                        yield content

                final_text = "".join(text_parts)
                if final_text and store_reply:
                    await store_reply(final_text)

            return ReplyType.SIMPLE_REPLY, stream_generator()

        if stream_kind == ReplyType.TOOL_CALL:
            async for event in response:
                self._merge_anthropic_stream_event(content_blocks, event)

            tool_calls = self._finalize_anthropic_stream_tool_calls(content_blocks)
            if tool_calls:
                return ReplyType.TOOL_CALL, tool_calls

            logger.warning("Anthropic stream response ended without tool output")
            return ReplyType.ERROR, ModelErrorEvent(
                code="empty_tool_response",
                message="No tool output from model response.",
            )

        logger.warning("Anthropic stream response contains no recognized output")
        return ReplyType.ERROR, ModelErrorEvent(
            code="empty_stream_response",
            message="No valid output from model response.",
        )

    # ---- Static helpers ----

    @staticmethod
    def _field(obj: Any, name: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(name, default)
        value = getattr(obj, name, default)
        if value is not default:
            return value
        model_extra = getattr(obj, "model_extra", None)
        if isinstance(model_extra, dict) and name in model_extra:
            return model_extra.get(name, default)
        return value

    @staticmethod
    def _normalize_backend(backend: str) -> str:
        normalized = str(backend or "openai").strip().lower()
        if normalized not in {"openai", "anthropic"}:
            raise ValueError("model backend must be one of: openai, anthropic")
        return normalized

    @staticmethod
    def _json_loads(value: Any) -> Any:
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _json_dumps(value: Any) -> str:
        try:
            return json.dumps(value if value is not None else {}, ensure_ascii=False)
        except TypeError:
            return json.dumps(str(value), ensure_ascii=False)

    @classmethod
    def _content_to_text(cls, content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts = []
            for item in content:
                item_type = cls._field(item, "type")
                if item_type == "text":
                    text = cls._field(item, "text")
                    if text:
                        text_parts.append(str(text))
                elif item_type == "tool_result":
                    text = cls._field(item, "content")
                    if text:
                        text_parts.append(str(text))
                elif isinstance(item, str):
                    text_parts.append(item)
            return "\n".join(text_parts)
        return str(content)

    @classmethod
    def _normalize_anthropic_content_blocks(cls, content: list) -> list[dict]:
        blocks = []
        for block in content:
            block_type = cls._field(block, "type")
            if not block_type:
                continue
            if block_type == "text":
                text = cls._field(block, "text")
                if text:
                    blocks.append({"type": "text", "text": str(text)})
                continue
            if block_type == "thinking":
                thinking = cls._field(block, "thinking")
                if thinking:
                    normalized = {"type": "thinking", "thinking": str(thinking)}
                    signature = cls._field(block, "signature")
                    if signature:
                        normalized["signature"] = signature
                    blocks.append(normalized)
                continue
            if block_type == "tool_use":
                blocks.append({
                    "type": "tool_use",
                    "id": cls._field(block, "id") or "call_0",
                    "name": cls._field(block, "name") or "",
                    "input": cls._field(block, "input") or {},
                })
                continue
            if isinstance(block, dict):
                blocks.append(dict(block))
        return blocks

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
        reasoning_content = ModelClient._field(message, "reasoning_content")
        return [
            ChatToolCall.from_raw(tool_call, reasoning_content=reasoning_content)
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
    def _extract_stream_reasoning_delta(chunk) -> str:
        """Return a DeepSeek thinking-mode reasoning delta when present."""
        for choice in ModelClient._chunk_choices(chunk):
            delta = ModelClient._field(choice, "delta")
            content = ModelClient._field(delta, "reasoning_content")
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
    def _extract_anthropic_response_text(response) -> str:
        """Extract assistant text from an Anthropic Messages response."""
        text_parts = []
        for block in ModelClient._field(response, "content") or []:
            if ModelClient._field(block, "type") == "text":
                text = ModelClient._field(block, "text")
                if text:
                    text_parts.append(str(text))
        return "".join(text_parts)

    @staticmethod
    def _extract_anthropic_tool_calls(response) -> list[ChatToolCall]:
        """Return normalized function tool calls from an Anthropic Messages response."""
        content = ModelClient._field(response, "content") or []
        content_blocks = ModelClient._normalize_anthropic_content_blocks(content)
        tool_calls = []
        for block in content:
            if ModelClient._field(block, "type") != "tool_use":
                continue
            tool_call = ChatToolCall.from_anthropic_block(
                block,
                content_blocks=content_blocks,
            )
            if tool_call.name:
                tool_calls.append(tool_call)
        return tool_calls

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
    def _finalize_stream_tool_calls(
        tool_call_parts: dict[int, dict],
        reasoning_content: Optional[str] = None,
    ) -> list[ChatToolCall]:
        tool_calls = []
        for index in sorted(tool_call_parts):
            part = tool_call_parts[index]
            if not part["id"]:
                part["id"] = f"call_{index}"
            tool_call = ChatToolCall.from_raw(part, reasoning_content=reasoning_content)
            if tool_call.name:
                tool_calls.append(tool_call)
        return tool_calls

    @staticmethod
    def _extract_anthropic_stream_text_delta(event) -> str:
        if ModelClient._field(event, "type") != "content_block_delta":
            return ""
        delta = ModelClient._field(event, "delta")
        if ModelClient._field(delta, "type") != "text_delta":
            return ""
        text = ModelClient._field(delta, "text")
        return text if isinstance(text, str) else ""

    @staticmethod
    def _anthropic_event_starts_tool_use(event) -> bool:
        if ModelClient._field(event, "type") != "content_block_start":
            return False
        block = ModelClient._field(event, "content_block")
        return ModelClient._field(block, "type") == "tool_use"

    @staticmethod
    def _merge_anthropic_stream_event(content_blocks: dict[int, dict], event) -> None:
        event_type = ModelClient._field(event, "type")
        index = ModelClient._field(event, "index")
        if index is None:
            return
        index = int(index)

        if event_type == "content_block_start":
            raw_block = ModelClient._field(event, "content_block")
            block_type = ModelClient._field(raw_block, "type")
            if block_type == "text":
                content_blocks[index] = {"type": "text", "text": ModelClient._field(raw_block, "text") or ""}
            elif block_type == "thinking":
                content_blocks[index] = {
                    "type": "thinking",
                    "thinking": ModelClient._field(raw_block, "thinking") or "",
                }
            elif block_type == "tool_use":
                content_blocks[index] = {
                    "type": "tool_use",
                    "id": ModelClient._field(raw_block, "id") or f"call_{index}",
                    "name": ModelClient._field(raw_block, "name") or "",
                    "input_json": "",
                }
            return

        if event_type != "content_block_delta":
            return

        delta = ModelClient._field(event, "delta")
        delta_type = ModelClient._field(delta, "type")
        block = content_blocks.setdefault(index, {"type": "text", "text": ""})
        if delta_type == "text_delta":
            block["type"] = "text"
            block["text"] = str(block.get("text") or "") + (ModelClient._field(delta, "text") or "")
        elif delta_type == "thinking_delta":
            block["type"] = "thinking"
            block["thinking"] = str(block.get("thinking") or "") + (ModelClient._field(delta, "thinking") or "")
        elif delta_type == "signature_delta":
            block["signature"] = ModelClient._field(delta, "signature") or ""
        elif delta_type == "input_json_delta":
            block["type"] = "tool_use"
            block["input_json"] = str(block.get("input_json") or "") + (
                ModelClient._field(delta, "partial_json") or ""
            )

    @staticmethod
    def _finalize_anthropic_stream_tool_calls(content_blocks: dict[int, dict]) -> list[ChatToolCall]:
        normalized_blocks = []
        raw_tool_blocks = []
        for index in sorted(content_blocks):
            block = dict(content_blocks[index])
            if block.get("type") == "tool_use":
                block["input"] = ModelClient._json_loads(block.pop("input_json", "") or "{}")
                raw_tool_blocks.append(block)
            normalized_blocks.append(block)

        tool_calls = []
        for block in raw_tool_blocks:
            tool_call = ChatToolCall.from_anthropic_block(
                block,
                content_blocks=normalized_blocks,
            )
            if tool_call.name:
                tool_calls.append(tool_call)
        return tool_calls
