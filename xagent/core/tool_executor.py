"""
ToolExecutor — tool execution engine with concurrency control and caching.

Responsibilities:
- Registering and deregistering tool callables
- Maintaining an invalidation-aware tool-spec cache
- Executing tool calls concurrently within a semaphore limit
- Building and returning the OpenAI function-call messages
- Factory helper ``make_http_agent_tool`` for HTTP sub-agent tools
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, Callable, Dict, List, Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from ..defaults import (
    DEFAULT_MAX_CONCURRENT_TOOLS,
    ERROR_RESPONSE_PREVIEW_LENGTH,
    RETRY_ATTEMPTS,
    RETRY_MIN_WAIT,
    RETRY_MAX_WAIT,
    TOOL_RESULT_PREVIEW_LENGTH,
)
from ..observability import observe
from ..schemas import Message, MessageType, RoleType, ToolCall
from ..utils.image_utils import extract_source, is_image_output
from ..utils.tool_decorator import function_tool

if TYPE_CHECKING:
    from .image_processor import ImageProcessor


def _preview(s: str) -> str:
    """Truncate *s* to the tool-result preview length for history storage."""
    return s if len(s) <= TOOL_RESULT_PREVIEW_LENGTH else s[:TOOL_RESULT_PREVIEW_LENGTH] + "..."


class ToolExecutor:
    """
    Tool execution engine.

    Holds the registry of user-provided and MCP-sourced tools, maintains a
    cached list of OpenAI tool-spec dicts, and handles concurrent execution.
    """

    def __init__(
        self,
        logger: Optional[logging.Logger] = None,
        image_processor: Optional[ImageProcessor] = None,
    ) -> None:
        self._tools: Dict[str, object] = {}
        self._mcp_tools: Dict[str, object] = {}
        self._mcp_tools_last_updated: Optional[float] = None
        self._cache: Optional[list] = None
        self._cache_updated: Optional[float] = None
        self.logger = logger or logging.getLogger(__name__)
        self.image_processor = image_processor

    # ------------------------------------------------------------------
    # Public read-only views
    # ------------------------------------------------------------------

    @property
    def tools(self) -> Dict[str, object]:
        return self._tools

    @property
    def mcp_tools(self) -> Dict[str, object]:
        return self._mcp_tools

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, tools: list) -> None:
        """
        Register tool callables.

        Each callable must be async and expose a ``tool_spec`` attribute.
        Duplicate names are silently ignored.
        """
        for fn in tools or []:
            if not asyncio.iscoroutinefunction(fn):
                raise TypeError(
                    f"Tool function '{fn.tool_spec['name']}' must be async."
                )
            if fn.tool_spec["name"] not in self._tools:
                self._tools[fn.tool_spec["name"]] = fn
        self._cache = None  # invalidate

    def update_mcp_tools(
        self, mcp_tools: Dict[str, object], last_updated: float
    ) -> None:
        """Replace the MCP tool set.  Only invalidates the cache on a real change."""
        if last_updated != self._mcp_tools_last_updated:
            self._mcp_tools = mcp_tools
            self._mcp_tools_last_updated = last_updated
            self._cache = None  # invalidate

    # ------------------------------------------------------------------
    # Tool-spec cache
    # ------------------------------------------------------------------

    @property
    def cached_specs(self) -> Optional[list]:
        """Return cached tool specs, rebuilding if stale."""
        if self._should_rebuild_cache():
            self._rebuild_cache()
        return self._cache

    def _should_rebuild_cache(self) -> bool:
        if self._cache is None:
            return True
        if self._mcp_tools_last_updated and (
            self._cache_updated is None
            or self._mcp_tools_last_updated > self._cache_updated
        ):
            return True
        return False

    def _rebuild_cache(self) -> None:
        all_tools = list(self._tools.values()) + list(self._mcp_tools.values())
        self._cache = [fn.tool_spec for fn in all_tools] if all_tools else None
        self._cache_updated = time.time()

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    @observe()
    async def handle_calls(
        self,
        tool_calls: list,
        message_storage: object,
        user_id: str,
        session_id: str,
        input_messages: list,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT_TOOLS,
    ) -> Optional[tuple]:
        """
        Execute all tool calls concurrently within a semaphore limit.

        Returns ``(image_data, description)`` if any tool produced image
        output, otherwise ``None``.  ``input_messages`` is extended in-place
        with the tool-call / tool-result message pairs.
        """
        if not tool_calls:
            return None

        function_calls = [
            tc for tc in tool_calls if getattr(tc, "type", None) == "function_call"
        ]
        if not function_calls:
            return None

        semaphore = asyncio.Semaphore(max_concurrent)

        async def _with_semaphore(tc):
            async with semaphore:
                return await self.execute(tc, message_storage, user_id, session_id)

        results = await asyncio.gather(*[_with_semaphore(tc) for tc in function_calls])

        pending_images: List[str] = []
        pending_descs: List[str] = []
        for tool_messages, image_data, description in results:
            if tool_messages:
                input_messages.extend([msg.to_dict() for msg in tool_messages])
            if image_data:
                pending_images.append(image_data)
            if description:
                pending_descs.append(description)

        if pending_images:
            return "\n\n".join(pending_images), "\n\n".join(pending_descs)
        return None

    async def execute(
        self,
        tool_call: object,
        message_storage: object,
        user_id: str,
        session_id: str,
    ) -> tuple:
        """
        Execute a single tool call and persist the call/result messages.

        Returns ``(messages_list, image_data_or_None, description_or_None)``.
        """
        name = getattr(tool_call, "name", None)
        try:
            args = json.loads(getattr(tool_call, "arguments", "{}"))
        except Exception as exc:
            self.logger.error("Tool args parse error: %s", exc)
            return None, None, None

        func = self._tools.get(name) or self._mcp_tools.get(name)
        if not func:
            return None, None, None

        self.logger.debug("Calling tool: %s with args: %s", name, args)
        try:
            result = await func(**args)
        except Exception as exc:
            self.logger.error("Tool call error: %s", exc)
            result = f"Tool error: {exc}"

        result_str = (
            json.dumps(result, ensure_ascii=False)
            if isinstance(result, (dict, list))
            else str(result)
        )

        image_data: Optional[str] = None
        model_output = result_str
        if is_image_output(result_str):
            image_data = result_str
            image_src = extract_source(result_str)
            prompt_hint = args.get("prompt", "")
            if self.image_processor:
                caption = await self.image_processor.caption(image_src, prompt_hint)
            else:
                caption = (
                    f'Generated image based on prompt: "{prompt_hint}"'
                    if prompt_hint
                    else "An image was generated."
                )
            model_output = (
                f"[Image generated by tool `{name}` and displayed to user. "
                f"Image description: {caption}]"
            )

        call_msg = Message(
            type=MessageType.FUNCTION_CALL,
            role=RoleType.TOOL,
            content=f"Calling tool: `{name}` with args: {args}",
            tool_call=ToolCall(
                call_id=getattr(tool_call, "call_id", ""),
                name=name,
                arguments=json.dumps(args, ensure_ascii=False),
            ),
        )
        result_msg = Message(
            type=MessageType.FUNCTION_CALL_OUTPUT,
            role=RoleType.TOOL,
            content=f"Tool `{name}` result: {_preview(result_str)}",
            tool_call=ToolCall(
                call_id=getattr(tool_call, "call_id", "001"),
                output=model_output,
            ),
        )
        await message_storage.add_messages(user_id, session_id, [call_msg, result_msg])
        return [call_msg, result_msg], image_data, model_output if image_data else None


# ---------------------------------------------------------------------------
# HTTP sub-agent tool factory
# ---------------------------------------------------------------------------

def make_http_agent_tool(
    server: str,
    name: Optional[str],
    description: Optional[str],
    get_http_client_fn: Callable,
    retry_attempts: int = RETRY_ATTEMPTS,
    retry_min_wait: float = RETRY_MIN_WAIT,
    retry_max_wait: float = RETRY_MAX_WAIT,
    error_preview_length: int = ERROR_RESPONSE_PREVIEW_LENGTH,
) -> Callable:
    """
    Build an async tool function that forwards calls to an HTTP agent server.

    Args:
        server: Base URL of the remote agent's HTTP server.
        name: Tool name exposed to the model.
        description: Tool description exposed to the model.
        get_http_client_fn: Zero-argument callable that returns an
            ``httpx.AsyncClient`` scoped to *server*.
        retry_attempts / retry_min_wait / retry_max_wait: Retry config.
        error_preview_length: Max characters included from HTTP error bodies.

    Returns:
        An async function decorated with ``tool_spec`` ready for registration.
    """

    @function_tool(
        name=name,
        description=description,
        param_descriptions={
            "input": (
                "A clear, focused instruction or question for the agent, "
                "sufficient to complete the task independently, with any "
                "necessary resources included."
            ),
            "expected_output": (
                "Specification of the desired output format, structure, or "
                "content type."
            ),
            "image_source": (
                "Optional list of image URLs, file paths, or base64 strings "
                "to be included in the message."
            ),
        },
    )
    async def tool_func(
        input: str,
        expected_output: str,
        image_source: Optional[List[str]] = None,
    ):
        user_message = f"### User Input:\n{input}"
        if expected_output:
            user_message += f"\n\n### Expected Output:\n{expected_output}"

        payload = {
            "user_id": f"http_agent_tool_{name or 'default'}_{uuid.uuid4().hex[:8]}",
            "session_id": f"session_{uuid.uuid4().hex[:8]}",
            "user_message": user_message,
            "stream": False,
        }
        if image_source:
            payload["image_source"] = image_source

        @retry(
            stop=stop_after_attempt(retry_attempts),
            wait=wait_exponential(
                multiplier=1, min=retry_min_wait, max=retry_max_wait
            ),
        )
        async def _request():
            return await get_http_client_fn().post("/chat", json=payload)

        try:
            response = await _request()
            if response.status_code == 200:
                data = response.json()
                reply = data.get("reply", "")
                return reply or "Empty reply from HTTP Agent"
            if response.status_code == 500:
                try:
                    detail = response.json().get("detail", "Internal server error")
                    return f"HTTP Agent internal error: {detail}"
                except Exception:
                    return f"HTTP Agent internal error: {response.text[:error_preview_length]}"
            return f"HTTP Agent error {response.status_code}: {response.text[:error_preview_length]}"
        except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError) as exc:
            kind = type(exc).__name__.replace("Exception", "").replace("Error", "")
            return f"HTTP Agent {kind.lower()}: {exc}"
        except json.JSONDecodeError as exc:
            return f"Invalid JSON response: {exc}"
        except Exception as exc:
            return f"Unexpected error: {exc}"

    return tool_func
