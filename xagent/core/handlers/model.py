import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Awaitable, Callable, Optional, Union

from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import AgentConfig, ReplyType
from ..providers import (
    MODEL_API_ANTHROPIC_MESSAGES,
    MODEL_API_OPENAI_CHAT_COMPLETIONS,
    MODEL_API_OPENAI_RESPONSES,
    PROVIDER_DEEPSEEK,
    PROVIDER_MINIMAX,
    PROVIDER_OPENAI,
    PROVIDER_QWEN,
    ReasoningConfig,
    normalize_model_api as normalize_provider_model_api,
    normalize_provider_name,
)


logger = logging.getLogger(__name__)


@dataclass
class ChatToolCall:
    """Provider-neutral function tool call used internally by the agent loop."""

    call_id: str
    name: str
    arguments: str
    assistant_content: Optional[str] = None
    reasoning_content: Optional[str] = None
    content_blocks: Optional[list[dict]] = None
    response_items: Optional[list[dict]] = None
    type: str = "function"

    @classmethod
    def from_raw(
        cls,
        raw_tool_call: Any,
        reasoning_content: Optional[str] = None,
        assistant_content: Optional[str] = None,
    ) -> "ChatToolCall":
        function = ModelClient._field(raw_tool_call, "function") or {}
        return cls(
            call_id=ModelClient._field(raw_tool_call, "id") or "",
            name=ModelClient._field(function, "name") or "",
            arguments=ModelClient._field(function, "arguments") or "{}",
            assistant_content=assistant_content,
            reasoning_content=reasoning_content,
            type=ModelClient._field(raw_tool_call, "type") or "function",
        )

    @classmethod
    def from_anthropic_block(
        cls,
        raw_tool_block: Any,
        content_blocks: Optional[list[dict]] = None,
        assistant_content: Optional[str] = None,
    ) -> "ChatToolCall":
        input_value = ModelClient._field(raw_tool_block, "input") or {}
        return cls(
            call_id=ModelClient._field(raw_tool_block, "id") or "",
            name=ModelClient._field(raw_tool_block, "name") or "",
            arguments=ModelClient._json_dumps(input_value),
            assistant_content=assistant_content,
            content_blocks=content_blocks,
            type="function",
        )

    @classmethod
    def from_responses_item(
        cls,
        raw_tool_call: Any,
        response_items: Optional[list[dict]] = None,
        assistant_content: Optional[str] = None,
    ) -> "ChatToolCall":
        return cls(
            call_id=ModelClient._field(raw_tool_call, "call_id") or ModelClient._field(raw_tool_call, "id") or "",
            name=ModelClient._field(raw_tool_call, "name") or "",
            arguments=ModelClient._field(raw_tool_call, "arguments") or "{}",
            assistant_content=assistant_content,
            response_items=response_items,
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


@dataclass
class ModelStreamEvent:
    """Provider-neutral streaming event emitted by a single model response."""

    type: str
    delta: str = ""
    tool_calls: list[ChatToolCall] = field(default_factory=list)
    error: Optional[ModelErrorEvent] = None


class ModelClient:
    """Handles model calls across Responses, Chat Completions, and Anthropic Messages."""

    def __init__(
        self,
        client: Any,
        model: str,
        model_api: str = MODEL_API_OPENAI_CHAT_COMPLETIONS,
        max_tokens: Optional[int] = None,
        provider_name: str = PROVIDER_OPENAI,
        reasoning: Optional[ReasoningConfig] = None,
    ):
        self.client = client
        self.model = model
        self.provider_name = normalize_provider_name(provider_name) or PROVIDER_OPENAI
        self.model_api = self._normalize_model_api(model_api)
        self.max_tokens = max_tokens
        self.reasoning = reasoning

    @retry(
        stop=stop_after_attempt(AgentConfig.RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=AgentConfig.RETRY_MIN_WAIT, max=AgentConfig.RETRY_MAX_WAIT)
    )
    async def call(
        self,
        messages: list,
        tool_specs: Optional[list],
        instructions: Optional[Union[str, list[dict]]] = None,
        stream: bool = False,
        store_reply: Optional[Callable[..., Awaitable]] = None,
    ) -> tuple[ReplyType, object]:
        """
        Call the AI model with prepared messages.

        Args:
            messages: Input message list (user/assistant/tool content only).
            tool_specs: Tool specifications for the model.
            instructions: Static behavioural instructions (system prompt).
            stream: Whether to stream the response.
            store_reply: Async callback to store the final reply text.
        Returns:
            Tuple of (ReplyType, response_object).
        """
        try:
            if self.model_api == MODEL_API_ANTHROPIC_MESSAGES:
                response = await self.client.messages.create(
                    **self._build_anthropic_create_params(
                        messages=messages,
                        tool_specs=tool_specs,
                        instructions=instructions,
                        stream=stream,
                    )
                )
                if stream:
                    return await self._handle_anthropic_stream(response, store_reply)
                return self._handle_anthropic_non_stream(response)

            if self.model_api == MODEL_API_OPENAI_RESPONSES:
                response = await self.client.responses.create(
                    **self._build_responses_create_params(
                        messages=messages,
                        tool_specs=tool_specs,
                        instructions=instructions,
                        stream=stream,
                    )
                )
                if stream:
                    return await self._handle_responses_stream(response, store_reply)
                return self._handle_responses_non_stream(response)

            response = await self.client.chat.completions.create(
                **self._build_create_params(
                    messages=messages,
                    tool_specs=tool_specs,
                    instructions=instructions,
                    stream=stream,
                )
            )

            if stream:
                return await self._handle_stream(response, store_reply)
            return self._handle_non_stream(response)

        except Exception as e:
            logger.exception("Model call failed: %s", e)
            return ReplyType.ERROR, ModelErrorEvent(
                code="model_call_failed",
                message="Model call failed.",
                details=str(e),
            )

    async def stream_turn(
        self,
        messages: list,
        tool_specs: Optional[list],
        instructions: Optional[Union[str, list[dict]]] = None,
    ) -> AsyncGenerator[ModelStreamEvent, None]:
        async for event in self.model_turn_events(
            messages=messages,
            tool_specs=tool_specs,
            instructions=instructions,
            stream=True,
        ):
            yield event

    async def model_turn_events(
        self,
        messages: list,
        tool_specs: Optional[list],
        instructions: Optional[Union[str, list[dict]]] = None,
        stream: bool = False,
    ) -> AsyncGenerator[ModelStreamEvent, None]:
        """Emit visible model text and finalized tool calls for one model turn."""
        try:
            if not stream:
                async for event in self._non_stream_turn_events(
                    messages=messages,
                    tool_specs=tool_specs,
                    instructions=instructions,
                ):
                    yield event
                return

            if self.model_api == MODEL_API_ANTHROPIC_MESSAGES:
                response = await self.client.messages.create(
                    **self._build_anthropic_create_params(
                        messages=messages,
                        tool_specs=tool_specs,
                        instructions=instructions,
                        stream=True,
                    )
                )
                async for event in self._iter_anthropic_turn_events(response):
                    yield event
                return

            if self.model_api == MODEL_API_OPENAI_RESPONSES:
                response = await self.client.responses.create(
                    **self._build_responses_create_params(
                        messages=messages,
                        tool_specs=tool_specs,
                        instructions=instructions,
                        stream=True,
                    )
                )
                async for event in self._iter_responses_turn_events(response):
                    yield event
                return

            response = await self.client.chat.completions.create(
                **self._build_create_params(
                    messages=messages,
                    tool_specs=tool_specs,
                    instructions=instructions,
                    stream=True,
                )
            )
            async for event in self._iter_chat_turn_events(response):
                yield event
        except Exception as exc:
            logger.exception("Model stream failed: %s", exc)
            yield ModelStreamEvent(
                type="error",
                error=ModelErrorEvent(
                    code="model_stream_failed",
                    message="Model stream failed.",
                    details=str(exc),
                ),
            )

    async def _non_stream_turn_events(
        self,
        messages: list,
        tool_specs: Optional[list],
        instructions: Optional[Union[str, list[dict]]] = None,
    ) -> AsyncGenerator[ModelStreamEvent, None]:
        if self.model_api == MODEL_API_ANTHROPIC_MESSAGES:
            response = await self.client.messages.create(
                **self._build_anthropic_create_params(
                    messages=messages,
                    tool_specs=tool_specs,
                    instructions=instructions,
                    stream=False,
                )
            )
            events = self._anthropic_non_stream_turn_events(response)
        elif self.model_api == MODEL_API_OPENAI_RESPONSES:
            response = await self.client.responses.create(
                **self._build_responses_create_params(
                    messages=messages,
                    tool_specs=tool_specs,
                    instructions=instructions,
                    stream=False,
                )
            )
            events = self._responses_non_stream_turn_events(response)
        else:
            response = await self.client.chat.completions.create(
                **self._build_create_params(
                    messages=messages,
                    tool_specs=tool_specs,
                    instructions=instructions,
                    stream=False,
                )
            )
            events = self._chat_non_stream_turn_events(response)

        for event in events:
            yield event

    def _build_create_params(
        self,
        messages: list,
        tool_specs: Optional[list],
        instructions: Optional[Union[str, list[dict]]],
        stream: bool,
    ) -> dict:
        params = {
            "model": self.model,
            "messages": self._build_chat_messages(messages, instructions),
            "stream": stream,
        }
        if tool_specs:
            params["tools"] = tool_specs
            params["tool_choice"] = "auto"
        if self.max_tokens is not None:
            if self.provider_name in {PROVIDER_DEEPSEEK, PROVIDER_QWEN}:
                params["max_tokens"] = self.max_tokens
            else:
                params["max_completion_tokens"] = self.max_tokens
        self._apply_chat_reasoning_params(params)
        return params

    def _build_responses_create_params(
        self,
        messages: list,
        tool_specs: Optional[list],
        instructions: Optional[Union[str, list[dict]]],
        stream: bool,
    ) -> dict:
        params = {
            "model": self.model,
            "input": self._build_responses_input(messages),
            "stream": stream,
            "store": False,
        }

        if isinstance(instructions, list):
            parts = [self._content_to_text(message.get("content")) for message in instructions]
            instruction_text = "\n\n".join(part for part in parts if part)
        else:
            instruction_text = instructions or ""
        if instruction_text:
            params["instructions"] = instruction_text
        if tool_specs:
            params["tools"] = self._to_responses_tools(tool_specs)
            params["tool_choice"] = "auto"
            params["include"] = ["reasoning.encrypted_content"]
        if self.max_tokens is not None:
            params["max_output_tokens"] = self.max_tokens
        if self.reasoning is not None:
            params["reasoning"] = {
                "effort": self.reasoning.effort if self.reasoning.enabled else "none"
            }
        return params

    def _apply_chat_reasoning_params(self, params: dict) -> None:
        reasoning = self.reasoning
        if reasoning is None:
            return
        if self.provider_name == PROVIDER_DEEPSEEK:
            params["extra_body"] = {
                "thinking": {"type": "enabled" if reasoning.enabled else "disabled"}
            }
            if reasoning.enabled:
                params["reasoning_effort"] = reasoning.effort
            return
        if self.provider_name == PROVIDER_QWEN:
            extra_body: dict[str, Any] = {"enable_thinking": reasoning.enabled}
            if reasoning.enabled:
                extra_body["thinking_budget"] = reasoning.budget_tokens
            params["extra_body"] = extra_body
            return
        params["reasoning_effort"] = reasoning.effort if reasoning.enabled else "none"

    @classmethod
    def _build_responses_input(cls, messages: list) -> list:
        input_items = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            message_type = str(message.get("type") or "").strip()
            if message_type in {"reasoning", "function_call", "function_call_output", "message"}:
                input_items.append(cls._to_plain_data(message))
                continue

            role = str(message.get("role") or "").strip()
            if role == "tool":
                input_items.append({
                    "type": "function_call_output",
                    "call_id": str(message.get("tool_call_id") or "call_0"),
                    "output": cls._content_to_text(message.get("content")),
                })
                continue

            if role == "assistant" and message.get("tool_calls"):
                content_text = cls._content_to_text(message.get("content"))
                if content_text:
                    input_items.append({"role": "assistant", "content": content_text})
                for tool_call in message.get("tool_calls") or []:
                    input_items.append(cls._chat_tool_call_to_responses_item(tool_call))
                continue

            if role in {"user", "assistant", "system"}:
                content = cls._to_responses_message_content(message.get("content"), role=role)
                if content:
                    input_items.append({"role": role, "content": content})
        return input_items

    @classmethod
    def _to_responses_message_content(cls, content: Any, *, role: str) -> Any:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return cls._content_to_text(content)

        blocks = []
        for item in content:
            item_type = cls._field(item, "type")
            if item_type == "text":
                text = cls._field(item, "text")
                if text:
                    blocks.append({"type": "input_text", "text": str(text)})
                continue
            if item_type == "image_url":
                image_url = cls._field(item, "image_url") or {}
                url = cls._field(image_url, "url")
                if url:
                    blocks.append({"type": "input_image", "image_url": str(url)})
                continue
            text = cls._content_to_text(item)
            if text:
                blocks.append({"type": "input_text", "text": text})
        return blocks or ""

    @classmethod
    def _chat_tool_call_to_responses_item(cls, tool_call: Any) -> dict:
        function = cls._field(tool_call, "function") or {}
        return {
            "type": "function_call",
            "call_id": cls._field(tool_call, "id") or cls._field(tool_call, "call_id") or "call_0",
            "name": cls._field(function, "name") or cls._field(tool_call, "name") or "",
            "arguments": cls._field(function, "arguments") or cls._field(tool_call, "arguments") or "{}",
        }

    @classmethod
    def _to_responses_tools(cls, tool_specs: list) -> list[dict]:
        tools = []
        for tool_spec in tool_specs:
            function = cls._field(tool_spec, "function") or tool_spec
            name = cls._field(function, "name")
            if not name:
                continue
            tool = {
                "type": "function",
                "name": name,
                "description": cls._field(function, "description") or "",
                "parameters": cls._field(function, "parameters") or {"type": "object"},
                "strict": bool(cls._field(function, "strict", False)),
            }
            tools.append(tool)
        return tools

    def _build_anthropic_create_params(
        self,
        messages: list,
        tool_specs: Optional[list],
        instructions: Optional[Union[str, list[dict]]],
        stream: bool,
    ) -> dict:
        system, anthropic_messages = self._build_anthropic_messages(
            messages=messages,
            instructions=instructions,
        )
        params = {
            "model": self.model,
            "max_tokens": self.max_tokens or AgentConfig.DEFAULT_MAX_TOKENS,
            "messages": anthropic_messages,
            "stream": stream,
        }
        if system:
            params["system"] = system
        if tool_specs:
            params["tools"] = self._to_anthropic_tools(tool_specs)
            params["tool_choice"] = {"type": "auto"}
        if self.reasoning is not None:
            if self.provider_name == PROVIDER_MINIMAX:
                raise ValueError("reasoning is not supported for the MiniMax Anthropic-compatible API")
            if not self.reasoning.enabled:
                params["thinking"] = {"type": "disabled"}
            elif self.reasoning.effort is not None:
                params["thinking"] = {"type": "adaptive", "display": "omitted"}
                params["output_config"] = {"effort": self.reasoning.effort}
            else:
                params["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": self.reasoning.budget_tokens,
                }
        return params

    @staticmethod
    def _build_chat_messages(
        messages: list,
        instructions: Optional[Union[str, list[dict]]],
        strip_provider_extras: bool = True,
    ) -> list:
        chat_messages = []
        if isinstance(instructions, list):
            chat_messages.extend(dict(message) for message in instructions)
        elif instructions:
            chat_messages.append({"role": "system", "content": instructions})
        chat_messages.extend(messages)
        chat_messages = ModelClient._coalesce_leading_system_messages(chat_messages)
        return ModelClient._strip_message_names(
            chat_messages,
            strip_provider_extras=strip_provider_extras,
        )

    @classmethod
    def _coalesce_leading_system_messages(cls, messages: list) -> list:
        """Merge consecutive leading system messages into one provider-safe message."""
        index = 0
        system_parts: list[str] = []

        while index < len(messages):
            message = messages[index]
            if not isinstance(message, dict):
                break
            role = str(message.get("role") or "").strip()
            if role != "system":
                break
            content_text = cls._content_to_text(message.get("content"))
            if content_text:
                system_parts.append(content_text)
            index += 1

        if index <= 1:
            return messages

        coalesced: list = []
        if system_parts:
            coalesced.append({
                "role": "system",
                "content": "\n\n".join(system_parts),
            })
        coalesced.extend(messages[index:])
        return coalesced

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
    ) -> tuple[str, list[dict]]:
        chat_messages = cls._build_chat_messages(
            messages,
            instructions,
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
    def _handle_non_stream(
        response,
    ) -> tuple[ReplyType, object]:
        """Handle a non-streaming model response."""
        text = ModelClient._extract_response_text(response)
        tool_calls = ModelClient._extract_tool_calls(response)
        if tool_calls:
            ModelClient._set_tool_calls_assistant_content(tool_calls, text or None)
            return ReplyType.TOOL_CALL, tool_calls

        if text:
            return ReplyType.SIMPLE_REPLY, text

        logger.warning("Model response contains no valid output: %s", response)
        return ReplyType.ERROR, ModelErrorEvent(
            code="empty_model_response",
            message="No valid output from model response.",
        )

    @staticmethod
    def _handle_responses_non_stream(
        response,
    ) -> tuple[ReplyType, object]:
        """Handle a non-streaming OpenAI Responses API response."""
        text = ModelClient._extract_responses_text(response)
        tool_calls = ModelClient._extract_responses_tool_calls(response)
        if tool_calls:
            ModelClient._set_tool_calls_assistant_content(tool_calls, text or None)
            return ReplyType.TOOL_CALL, tool_calls

        if text:
            return ReplyType.SIMPLE_REPLY, text

        logger.warning("Responses API response contains no valid output: %s", response)
        return ReplyType.ERROR, ModelErrorEvent(
            code="empty_model_response",
            message="No valid output from model response.",
        )

    @staticmethod
    def _handle_anthropic_non_stream(
        response,
    ) -> tuple[ReplyType, object]:
        """Handle a non-streaming Anthropic Messages response."""
        text = ModelClient._extract_anthropic_response_text(response)
        tool_calls = ModelClient._extract_anthropic_tool_calls(response)
        if tool_calls:
            ModelClient._set_tool_calls_assistant_content(tool_calls, text or None)
            return ReplyType.TOOL_CALL, tool_calls

        if text:
            return ReplyType.SIMPLE_REPLY, text

        logger.warning("Anthropic response contains no valid output: %s", response)
        return ReplyType.ERROR, ModelErrorEvent(
            code="empty_model_response",
            message="No valid output from model response.",
        )

    @classmethod
    def _chat_non_stream_turn_events(cls, response) -> list[ModelStreamEvent]:
        text = cls._extract_response_text(response)
        tool_calls = cls._extract_tool_calls(response)
        return cls._completed_turn_events(text, tool_calls)

    @classmethod
    def _responses_non_stream_turn_events(cls, response) -> list[ModelStreamEvent]:
        text = cls._extract_responses_text(response)
        tool_calls = cls._extract_responses_tool_calls(response)
        return cls._completed_turn_events(text, tool_calls)

    @classmethod
    def _anthropic_non_stream_turn_events(cls, response) -> list[ModelStreamEvent]:
        text = cls._extract_anthropic_response_text(response)
        tool_calls = cls._extract_anthropic_tool_calls(response)
        return cls._completed_turn_events(text, tool_calls)

    @classmethod
    def _completed_turn_events(
        cls,
        text: str,
        tool_calls: list[ChatToolCall],
    ) -> list[ModelStreamEvent]:
        events: list[ModelStreamEvent] = []
        if text:
            cls._set_tool_calls_assistant_content(tool_calls, text)
            events.append(ModelStreamEvent(type="text", delta=text))
        if tool_calls:
            events.append(ModelStreamEvent(type="tool_calls", tool_calls=tool_calls))
        if not events:
            events.append(ModelStreamEvent(
                type="error",
                error=ModelErrorEvent(
                    code="empty_model_response",
                    message="No valid output from model response.",
                ),
            ))
        return events

    async def _handle_stream(
        self,
        response,
        store_reply: Optional[Callable[..., Awaitable]] = None,
    ) -> tuple[ReplyType, object]:
        """Handle a streaming model response."""
        return await self._collect_stream_result(
            self._iter_chat_turn_events(response),
            store_reply,
        )

    async def _handle_responses_stream(
        self,
        response,
        store_reply: Optional[Callable[..., Awaitable]] = None,
    ) -> tuple[ReplyType, object]:
        """Handle a streaming OpenAI Responses API response."""
        return await self._collect_stream_result(
            self._iter_responses_turn_events(response),
            store_reply,
        )

    async def _handle_anthropic_stream(
        self,
        response,
        store_reply: Optional[Callable[..., Awaitable]] = None,
    ) -> tuple[ReplyType, object]:
        """Handle a streaming Anthropic Messages response."""
        return await self._collect_stream_result(
            self._iter_anthropic_turn_events(response),
            store_reply,
        )

    async def _collect_stream_result(
        self,
        events: AsyncGenerator[ModelStreamEvent, None],
        store_reply: Optional[Callable[..., Awaitable]] = None,
    ) -> tuple[ReplyType, object]:
        """Collect model stream events into the ReplyType contract."""
        text_parts: list[str] = []
        async for event in events:
            if event.type in {"delta", "text"} and event.delta:
                text_parts.append(event.delta)
                continue
            if event.type == "tool_calls":
                return ReplyType.TOOL_CALL, event.tool_calls
            if event.type == "error":
                return ReplyType.ERROR, event.error or ModelErrorEvent(
                    code="model_stream_error",
                    message="Model stream failed.",
                )

        if text_parts:
            async def stream_generator():
                for part in text_parts:
                    yield part
                final_text = "".join(text_parts)
                if final_text and store_reply:
                    await store_reply(final_text)

            return ReplyType.SIMPLE_REPLY, stream_generator()

        logger.warning("Stream response contains no recognized output")
        return ReplyType.ERROR, ModelErrorEvent(
            code="empty_stream_response",
            message="No valid output from model response.",
        )

    async def _iter_chat_turn_events(self, response) -> AsyncGenerator[ModelStreamEvent, None]:
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_call_parts: dict[int, dict] = {}

        async for chunk in response:
            reasoning_delta = self._extract_stream_reasoning_delta(chunk)
            if reasoning_delta:
                reasoning_parts.append(reasoning_delta)

            self._merge_stream_tool_calls(tool_call_parts, chunk)

            content = self._extract_stream_text_delta(chunk)
            if content:
                text_parts.append(content)
                yield ModelStreamEvent(type="delta", delta=content)

        reasoning_content = "".join(reasoning_parts) if reasoning_parts else None
        assistant_content = "".join(text_parts) or None
        tool_calls = self._finalize_stream_tool_calls(
            tool_call_parts,
            reasoning_content=reasoning_content,
            assistant_content=assistant_content,
        )
        if tool_calls:
            yield ModelStreamEvent(type="tool_calls", tool_calls=tool_calls)
            return

        if not text_parts:
            yield ModelStreamEvent(
                type="error",
                error=ModelErrorEvent(
                    code="empty_stream_response",
                    message="No valid output from model response.",
                ),
            )

    async def _iter_responses_turn_events(self, response) -> AsyncGenerator[ModelStreamEvent, None]:
        text_parts: list[str] = []
        tool_call_parts: dict[int, dict] = {}
        response_items: list[dict] = []
        completed_response = None

        async for event in response:
            completed_response = self._merge_responses_stream_event(
                tool_call_parts,
                response_items,
                event,
            ) or completed_response

            content = self._extract_responses_stream_text_delta(event)
            if content:
                text_parts.append(content)
                yield ModelStreamEvent(type="delta", delta=content)

        assistant_content = "".join(text_parts) or None
        tool_calls = self._finalize_responses_stream_tool_calls(
            tool_call_parts,
            response_items,
            assistant_content=assistant_content,
        )
        if not tool_calls and completed_response is not None:
            tool_calls = self._extract_responses_tool_calls(completed_response)
            if assistant_content:
                self._set_tool_calls_assistant_content(tool_calls, assistant_content)

        if tool_calls:
            yield ModelStreamEvent(type="tool_calls", tool_calls=tool_calls)
            return

        if not text_parts and completed_response is not None:
            text = self._extract_responses_text(completed_response)
            if text:
                yield ModelStreamEvent(type="delta", delta=text)
                return

        if not text_parts:
            yield ModelStreamEvent(
                type="error",
                error=ModelErrorEvent(
                    code="empty_stream_response",
                    message="No valid output from model response.",
                ),
            )

    async def _iter_anthropic_turn_events(self, response) -> AsyncGenerator[ModelStreamEvent, None]:
        text_parts: list[str] = []
        content_blocks: dict[int, dict] = {}

        async for event in response:
            self._merge_anthropic_stream_event(content_blocks, event)
            content = self._extract_anthropic_stream_text_delta(event)
            if content:
                text_parts.append(content)
                yield ModelStreamEvent(type="delta", delta=content)

        assistant_content = "".join(text_parts) or None
        tool_calls = self._finalize_anthropic_stream_tool_calls(
            content_blocks,
            assistant_content=assistant_content,
        )
        if tool_calls:
            yield ModelStreamEvent(type="tool_calls", tool_calls=tool_calls)
            return

        if not text_parts:
            yield ModelStreamEvent(
                type="error",
                error=ModelErrorEvent(
                    code="empty_stream_response",
                    message="No valid output from model response.",
                ),
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

    @classmethod
    def _to_plain_data(cls, value: Any) -> Any:
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return cls._to_plain_data(model_dump(exclude_none=True))
        if isinstance(value, dict):
            return {
                key: cls._to_plain_data(item)
                for key, item in value.items()
                if item is not None
            }
        if hasattr(value, "__dict__"):
            return {
                key: cls._to_plain_data(item)
                for key, item in vars(value).items()
                if item is not None
            }
        if isinstance(value, list):
            return [cls._to_plain_data(item) for item in value if item is not None]
        return value

    @staticmethod
    def _normalize_model_api(model_api: Optional[str]) -> str:
        return normalize_provider_model_api(model_api or MODEL_API_OPENAI_CHAT_COMPLETIONS)

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
                signature = cls._field(block, "signature")
                normalized = {
                    "type": "thinking",
                    "thinking": str(thinking) if thinking is not None else "",
                }
                if signature:
                    normalized["signature"] = signature
                blocks.append(normalized)
                continue
            if block_type == "redacted_thinking":
                normalized = cls._to_plain_data(block)
                if isinstance(normalized, dict):
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

    @classmethod
    def _text_from_anthropic_blocks(cls, content_blocks: list[dict]) -> str:
        text_parts = []
        for block in content_blocks:
            if cls._field(block, "type") != "text":
                continue
            text = cls._field(block, "text")
            if text:
                text_parts.append(str(text))
        return "".join(text_parts)

    @staticmethod
    def _set_tool_calls_assistant_content(
        tool_calls: list[ChatToolCall],
        assistant_content: Optional[str],
    ) -> None:
        if not assistant_content:
            return
        for tool_call in tool_calls:
            tool_call.assistant_content = assistant_content

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
        assistant_content = ModelClient._extract_response_text(response) or None
        return [
            ChatToolCall.from_raw(
                tool_call,
                reasoning_content=reasoning_content,
                assistant_content=assistant_content,
            )
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
    def _extract_responses_text(response, fallback_text: str = "") -> str:
        """Extract assistant text from a completed Responses API response."""
        output_text = ModelClient._field(response, "output_text")
        if isinstance(output_text, str) and output_text:
            return output_text

        text_parts = []
        for output_item in ModelClient._field(response, "output", []) or []:
            if ModelClient._field(output_item, "type") != "message":
                continue
            for content_item in ModelClient._field(output_item, "content", []) or []:
                text = ModelClient._field(content_item, "text")
                if isinstance(text, str) and text:
                    text_parts.append(text)
        return "".join(text_parts) or fallback_text

    @staticmethod
    def _extract_responses_tool_calls(response) -> list[ChatToolCall]:
        """Return normalized function calls from a completed Responses API response."""
        raw_output = ModelClient._field(response, "output", []) or []
        response_items = ModelClient._to_plain_data(raw_output)
        assistant_content = ModelClient._extract_responses_text(response) or None
        tool_calls = []
        for output_item in raw_output:
            if ModelClient._field(output_item, "type") != "function_call":
                continue
            tool_call = ChatToolCall.from_responses_item(
                output_item,
                response_items=response_items,
                assistant_content=assistant_content,
            )
            if tool_call.name:
                tool_calls.append(tool_call)
        return tool_calls

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
        assistant_content = ModelClient._text_from_anthropic_blocks(content_blocks) or None
        tool_calls = []
        for block in content:
            if ModelClient._field(block, "type") != "tool_use":
                continue
            tool_call = ChatToolCall.from_anthropic_block(
                block,
                content_blocks=content_blocks,
                assistant_content=assistant_content,
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
        assistant_content: Optional[str] = None,
    ) -> list[ChatToolCall]:
        tool_calls = []
        for index in sorted(tool_call_parts):
            part = tool_call_parts[index]
            if not part["id"]:
                part["id"] = f"call_{index}"
            tool_call = ChatToolCall.from_raw(
                part,
                reasoning_content=reasoning_content,
                assistant_content=assistant_content,
            )
            if tool_call.name:
                tool_calls.append(tool_call)
        return tool_calls

    @staticmethod
    def _extract_responses_stream_text_delta(event) -> str:
        event_type = ModelClient._field(event, "type")
        if event_type not in {"response.output_text.delta", "response.text.delta"}:
            return ""
        delta = ModelClient._field(event, "delta")
        return delta if isinstance(delta, str) else ""

    @staticmethod
    def _responses_event_starts_tool_call(event) -> bool:
        if ModelClient._field(event, "type") != "response.output_item.added":
            return False
        item = ModelClient._field(event, "item")
        return ModelClient._field(item, "type") == "function_call"

    @classmethod
    def _merge_responses_stream_event(
        cls,
        tool_call_parts: dict[int, dict],
        response_items: list[dict],
        event,
    ) -> Any:
        event_type = cls._field(event, "type")
        if event_type == "response.completed":
            return cls._field(event, "response")

        if event_type == "response.output_item.added":
            raw_item = cls._field(event, "item") or {}
            item = cls._to_plain_data(raw_item)
            if item:
                response_items.append(item)
            if cls._field(raw_item, "type") == "function_call":
                index = cls._field(event, "output_index")
                if index is None:
                    index = len(tool_call_parts)
                part = tool_call_parts.setdefault(int(index), cls._empty_responses_tool_part())
                cls._merge_responses_tool_item(part, raw_item)
            return None

        if event_type == "response.function_call_arguments.delta":
            index = cls._field(event, "output_index")
            if index is None:
                index = len(tool_call_parts)
            part = tool_call_parts.setdefault(int(index), cls._empty_responses_tool_part())
            item_id = cls._field(event, "item_id")
            if item_id:
                part["id"] = item_id
            delta = cls._field(event, "delta")
            if isinstance(delta, str):
                part["arguments"] += delta
            return None

        if event_type == "response.function_call_arguments.done":
            index = cls._field(event, "output_index")
            if index is None:
                index = len(tool_call_parts)
            part = tool_call_parts.setdefault(int(index), cls._empty_responses_tool_part())
            raw_item = cls._field(event, "item")
            if raw_item is not None:
                cls._merge_responses_tool_item(part, raw_item, replace_arguments=True)
                return None
            arguments = cls._field(event, "arguments")
            if isinstance(arguments, str):
                part["arguments"] = arguments
            return None

        return None

    @staticmethod
    def _empty_responses_tool_part() -> dict:
        return {
            "id": "",
            "type": "function_call",
            "call_id": "",
            "name": "",
            "arguments": "",
        }

    @classmethod
    def _merge_responses_tool_item(
        cls,
        part: dict,
        raw_item: Any,
        *,
        replace_arguments: bool = False,
    ) -> None:
        item_id = cls._field(raw_item, "id")
        if item_id:
            part["id"] = item_id
        call_id = cls._field(raw_item, "call_id")
        if call_id:
            part["call_id"] = call_id
        name = cls._field(raw_item, "name")
        if name:
            part["name"] = name
        arguments = cls._field(raw_item, "arguments")
        if isinstance(arguments, str):
            if replace_arguments:
                part["arguments"] = arguments
            else:
                part["arguments"] += arguments

    @classmethod
    def _finalize_responses_stream_tool_calls(
        cls,
        tool_call_parts: dict[int, dict],
        response_items: list[dict],
        assistant_content: Optional[str] = None,
    ) -> list[ChatToolCall]:
        function_items = []
        for index in sorted(tool_call_parts):
            part = dict(tool_call_parts[index])
            if not part.get("call_id"):
                part["call_id"] = part.get("id") or f"call_{index}"
            if not part.get("arguments"):
                part["arguments"] = "{}"
            function_items.append(part)

        replay_items = [item for item in response_items if item.get("type") != "function_call"]
        if assistant_content and not cls._response_items_have_message_text(replay_items):
            replay_items.append({
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": assistant_content}],
            })
        replay_items.extend(function_items)
        tool_calls = []
        for item in function_items:
            tool_call = ChatToolCall.from_responses_item(
                item,
                response_items=replay_items,
                assistant_content=assistant_content,
            )
            if tool_call.name:
                tool_calls.append(tool_call)
        return tool_calls

    @classmethod
    def _response_items_have_message_text(cls, response_items: list[dict]) -> bool:
        for item in response_items:
            if cls._field(item, "type") != "message":
                continue
            for content_item in cls._field(item, "content", []) or []:
                if cls._field(content_item, "text"):
                    return True
        return False

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
                signature = ModelClient._field(raw_block, "signature")
                if signature:
                    content_blocks[index]["signature"] = signature
            elif block_type == "redacted_thinking":
                normalized = ModelClient._to_plain_data(raw_block)
                if isinstance(normalized, dict):
                    content_blocks[index] = normalized
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
    def _finalize_anthropic_stream_tool_calls(
        content_blocks: dict[int, dict],
        assistant_content: Optional[str] = None,
    ) -> list[ChatToolCall]:
        normalized_blocks = []
        raw_tool_blocks = []
        for index in sorted(content_blocks):
            block = dict(content_blocks[index])
            if block.get("type") == "tool_use":
                block["input"] = ModelClient._json_loads(block.pop("input_json", "") or "{}")
                raw_tool_blocks.append(block)
            normalized_blocks.append(block)

        if assistant_content is None:
            assistant_content = ModelClient._text_from_anthropic_blocks(normalized_blocks) or None

        tool_calls = []
        for block in raw_tool_blocks:
            tool_call = ChatToolCall.from_anthropic_block(
                block,
                content_blocks=normalized_blocks,
                assistant_content=assistant_content,
            )
            if tool_call.name:
                tool_calls.append(tool_call)
        return tool_calls
