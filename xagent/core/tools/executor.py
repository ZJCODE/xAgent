import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from ..config import AgentConfig
from .manager import ToolManager
from ..ports import MessageStore
from ...utils.image_utils import is_image_output
from ...tools.image_generation_tool import (
    generated_image_attachments,
    generated_image_description,
    is_generated_image_result,
)
from ...tools.artifact_tool import (
    artifact_attachment_description,
    artifact_attachments,
    is_artifact_attachment_result,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolDisplayResult:
    """Displayable output from tool execution."""

    content: str
    description: str
    attachments: list[dict] = field(default_factory=list)


class ToolExecutor:
    """Executes tool calls, handles image display results, and manages concurrent execution."""

    def __init__(
        self,
        tool_manager: ToolManager,
        message_store: MessageStore,
        client: Any,
    ):
        self.tool_manager = tool_manager
        self.message_storage = message_store
        self.client = client

    async def handle_tool_calls(
        self,
        tool_calls: list,
        input_messages: list,
        max_concurrent_tools: int = AgentConfig.DEFAULT_MAX_CONCURRENT_TOOLS,
    ) -> Optional[ToolDisplayResult]:
        """
        Handle tool calls by executing them concurrently with concurrency limit.

        Returns:
            None if no displayable output, otherwise a ToolDisplayResult.
        """
        if not tool_calls:
            return None

        function_calls = [tc for tc in tool_calls if self._tool_name(tc)]
        if not function_calls:
            return None

        is_responses_call = self._has_responses_replay_items(function_calls)
        if is_responses_call:
            input_messages.extend(self._responses_replay_items(function_calls))
        else:
            assistant_content = self._tool_assistant_content(function_calls[0])
            assistant_message = {
                "role": "assistant",
                "content": assistant_content if assistant_content else None,
                "tool_calls": [self._to_chat_tool_call(tc) for tc in function_calls],
            }
            reasoning_content = self._tool_reasoning_content(function_calls[0])
            if reasoning_content is not None:
                assistant_message["reasoning_content"] = reasoning_content
            content_blocks = self._tool_content_blocks(function_calls[0])
            if content_blocks is not None:
                assistant_message["content_blocks"] = content_blocks
            input_messages.append(assistant_message)

        semaphore = asyncio.Semaphore(max_concurrent_tools)

        async def execute_with_semaphore(tool_call):
            async with semaphore:
                return await self.execute_single(tool_call)

        tasks = [execute_with_semaphore(tc) for tc in function_calls]
        results = await asyncio.gather(*tasks)

        pending_contents = []
        pending_descriptions = []
        pending_attachments = []

        for tool_message, display_result in results:
            input_messages.append(
                self._to_responses_tool_result(tool_message)
                if is_responses_call
                else tool_message
            )
            if display_result is None:
                continue
            if display_result.content:
                pending_contents.append(display_result.content)
            if display_result.description:
                pending_descriptions.append(display_result.description)
            pending_attachments.extend(display_result.attachments)

        if pending_contents or pending_attachments:
            return ToolDisplayResult(
                content="\n\n".join(pending_contents),
                description="\n\n".join(pending_descriptions),
                attachments=self._dedupe_attachments(pending_attachments),
            )

        return None

    async def execute_single(
        self,
        tool_call,
    ) -> tuple[dict, Optional[ToolDisplayResult]]:
        """Execute a single tool call and return (tool_message, display_result)."""
        name = self._tool_name(tool_call)
        call_id = self._tool_call_id(tool_call)
        raw_arguments = self._tool_arguments(tool_call)

        try:
            args = json.loads(raw_arguments or "{}")
        except Exception as e:
            logger.error("Tool args parse error: %s", e)
            return self._tool_result_message(call_id, f"Tool args parse error: {e}"), None

        if not isinstance(args, dict):
            return self._tool_result_message(call_id, "Tool args must be a JSON object."), None

        func = self.tool_manager.get_tool(name)
        if not func:
            return self._tool_result_message(call_id, f"Tool `{name}` not found."), None

        logger.info("Calling tool: %s with args: %s", name, args)

        try:
            result = await func(**args)
        except Exception as e:
            logger.error("Tool call error: %s", e)
            result = f"Tool error: {e}"

        if is_generated_image_result(result):
            result_str = json.dumps(result, ensure_ascii=False)
            model_output = generated_image_description(name, result)
            logger.info("Tool `%s` result: %s", name, self._format_preview(result_str))
            return self._tool_result_message(call_id, model_output), ToolDisplayResult(
                content="",
                description=model_output,
                attachments=generated_image_attachments(result),
            )

        if is_artifact_attachment_result(result):
            result_str = json.dumps(result, ensure_ascii=False)
            model_output = artifact_attachment_description(name, result)
            logger.info("Tool `%s` result: %s", name, self._format_preview(result_str))
            return self._tool_result_message(call_id, model_output), ToolDisplayResult(
                content="",
                description=model_output,
                attachments=artifact_attachments(result),
            )

        result_str = json.dumps(result, ensure_ascii=False) if isinstance(result, (dict, list)) else str(result)

        image_data = None
        model_output = result_str
        if is_image_output(result_str):
            image_data = result_str
            prompt_hint = args.get("prompt", "")
            model_output = self._image_result_description(name, prompt_hint)

        logger.info("Tool `%s` result: %s", name, self._format_preview(result_str))
        display_result = ToolDisplayResult(
            content=image_data,
            description=model_output,
            attachments=[],
        ) if image_data else None
        return self._tool_result_message(call_id, model_output), display_result

    @staticmethod
    def _dedupe_attachments(attachments: list[dict]) -> list[dict]:
        deduped: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            path = str(attachment.get("path") or "").strip()
            blob_url = str(attachment.get("blob_url") or "").strip()
            key = ("path", path) if path else ("blob_url", blob_url)
            if not key[1] or key in seen:
                continue
            seen.add(key)
            deduped.append(attachment)
        return deduped

    @staticmethod
    def _image_result_description(tool_name: str, prompt_hint: Any = "") -> str:
        description = f"[Image returned by tool `{tool_name}` and displayed to user."
        prompt = str(prompt_hint or "").strip()
        if prompt:
            description += f" Prompt: {prompt}."
        return description + "]"

    @staticmethod
    def _field(obj: Any, name: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)

    @classmethod
    def _tool_call_id(cls, tool_call) -> str:
        return cls._field(tool_call, "call_id") or cls._field(tool_call, "id") or "call_0"

    @classmethod
    def _tool_name(cls, tool_call) -> Optional[str]:
        name = cls._field(tool_call, "name")
        if name:
            return name
        function = cls._field(tool_call, "function") or {}
        return cls._field(function, "name")

    @classmethod
    def _tool_arguments(cls, tool_call) -> str:
        arguments = cls._field(tool_call, "arguments")
        if arguments is not None:
            return arguments
        function = cls._field(tool_call, "function") or {}
        return cls._field(function, "arguments") or "{}"

    @classmethod
    def _tool_reasoning_content(cls, tool_call) -> Optional[str]:
        return cls._field(tool_call, "reasoning_content")

    @classmethod
    def _tool_assistant_content(cls, tool_call) -> Optional[str]:
        content = cls._field(tool_call, "assistant_content")
        return content if isinstance(content, str) and content else None

    @classmethod
    def _tool_content_blocks(cls, tool_call) -> Optional[list[dict]]:
        return cls._field(tool_call, "content_blocks")

    @classmethod
    def _tool_response_items(cls, tool_call) -> Optional[list[dict]]:
        return cls._field(tool_call, "response_items")

    @classmethod
    def _has_responses_replay_items(cls, tool_calls: list) -> bool:
        return any(cls._tool_response_items(tool_call) for tool_call in tool_calls)

    @classmethod
    def _responses_replay_items(cls, tool_calls: list) -> list[dict]:
        for tool_call in tool_calls:
            response_items = cls._tool_response_items(tool_call)
            if response_items:
                return [dict(item) for item in response_items]
        return []

    @classmethod
    def _to_chat_tool_call(cls, tool_call) -> dict:
        return {
            "id": cls._tool_call_id(tool_call),
            "type": "function",
            "function": {
                "name": cls._tool_name(tool_call),
                "arguments": cls._tool_arguments(tool_call) or "{}",
            },
        }

    @staticmethod
    def _tool_result_message(call_id: str, content: str) -> dict:
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "content": content,
        }

    @staticmethod
    def _to_responses_tool_result(tool_message: dict) -> dict:
        return {
            "type": "function_call_output",
            "call_id": tool_message.get("tool_call_id") or "call_0",
            "output": tool_message.get("content") or "",
        }

    @staticmethod
    def _format_preview(text: str) -> str:
        if len(text) <= AgentConfig.TOOL_RESULT_PREVIEW_LENGTH:
            return text
        return text[:AgentConfig.TOOL_RESULT_PREVIEW_LENGTH] + "..."
