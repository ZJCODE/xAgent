"""
Agent — conversation orchestration (< 300 lines).

This module is intentionally lean: it wires together the focused
sub-components and owns only the conversation loop, memory management,
message storage, background tasks, and sub-agent tool creation.

Sub-components:
    ModelCaller      — OpenAI API calls and retry logic
    ToolExecutor     — tool registration, caching, and concurrent execution
    MCPManager       — MCP server connections and tool discovery
    ImageProcessor   — vision-model image captioning
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any, AsyncGenerator, Awaitable, Callable, List, Optional, Union

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel

# Base-class types only — imported here for type-checking / isinstance guards.
# The concrete Local implementations are imported lazily inside __init__ so
# that optional heavy dependencies (e.g. chromadb, langfuse) are never pulled
# in unless the caller actually needs them.
if TYPE_CHECKING:
    from ..components import MemoryStorageBase, MessageStorageBase

from ..defaults import (
    BACKGROUND_TASK_ATTEMPTS,
    BACKGROUND_TASK_BASE_DELAY,
    DEFAULT_AGENT_NAME,
    DEFAULT_HISTORY_COUNT,
    DEFAULT_MAX_BACKGROUND_TASKS,
    DEFAULT_MAX_CONCURRENT_TOOLS,
    DEFAULT_MAX_ITER,
    DEFAULT_MODEL,
    DEFAULT_SESSION_ID,
    DEFAULT_USER_ID,
    ERROR_RESPONSE_PREVIEW_LENGTH,
    HTTP_TIMEOUT,
    IMAGE_CAPTION_MODEL,
    IMAGE_CAPTION_PROMPT,
    MCP_CACHE_TTL,
    RETRY_ATTEMPTS,
    RETRY_MAX_WAIT,
    RETRY_MIN_WAIT,
    TOOL_RESULT_PREVIEW_LENGTH,
)
from ..observability import get_openai_client, observe
from ..schemas import Message, RoleType
from ..utils.image_utils import extract_image_urls_from_text
from ..utils.tool_decorator import function_tool
from .image_processor import ImageProcessor
from .mcp_manager import MCPManager
from .model_caller import ModelCaller, ReplyType
from .session import normalize_session_id as _normalize_session_id
from .tool_executor import ToolExecutor, make_http_agent_tool


class AgentConfig:
    """Configuration constants for the Agent class."""

    DEFAULT_NAME = DEFAULT_AGENT_NAME
    DEFAULT_MODEL = DEFAULT_MODEL
    DEFAULT_USER_ID = DEFAULT_USER_ID
    DEFAULT_SESSION_ID = DEFAULT_SESSION_ID
    DEFAULT_HISTORY_COUNT = DEFAULT_HISTORY_COUNT
    DEFAULT_MAX_ITER = DEFAULT_MAX_ITER
    DEFAULT_MAX_CONCURRENT_TOOLS = DEFAULT_MAX_CONCURRENT_TOOLS
    MCP_CACHE_TTL = MCP_CACHE_TTL
    HTTP_TIMEOUT = HTTP_TIMEOUT
    TOOL_RESULT_PREVIEW_LENGTH = TOOL_RESULT_PREVIEW_LENGTH
    ERROR_RESPONSE_PREVIEW_LENGTH = ERROR_RESPONSE_PREVIEW_LENGTH
    IMAGE_CAPTION_MODEL = IMAGE_CAPTION_MODEL
    IMAGE_CAPTION_PROMPT = IMAGE_CAPTION_PROMPT
    RETRY_ATTEMPTS = RETRY_ATTEMPTS
    RETRY_MIN_WAIT = RETRY_MIN_WAIT
    RETRY_MAX_WAIT = RETRY_MAX_WAIT
    BACKGROUND_TASK_ATTEMPTS = BACKGROUND_TASK_ATTEMPTS
    BACKGROUND_TASK_BASE_DELAY = BACKGROUND_TASK_BASE_DELAY
    DEFAULT_MAX_BACKGROUND_TASKS = DEFAULT_MAX_BACKGROUND_TASKS
    DEFAULT_SYSTEM_PROMPT = "**Context Information:**\n"


class Agent:
    """
    AI agent that orchestrates multi-turn conversations.

    Capabilities: tool calling, MCP server integration, sub-agent
    delegation, structured output, streaming responses, memory,
    and multi-user / shared-session modes.
    """

    def __init__(
        self,
        name: Optional[str] = None,
        system_prompt: Optional[str] = None,
        description: Optional[str] = None,
        model: Optional[str] = None,
        client: Optional[AsyncOpenAI] = None,
        tools: Optional[List] = None,
        mcp_servers: Optional[Union[str, List[str]]] = None,
        sub_agents: Optional[List[Union[tuple, "Agent"]]] = None,
        output_type: Optional[type[BaseModel]] = None,
        message_storage=None,
        memory_storage=None,
    ) -> None:
        self.name = name or AgentConfig.DEFAULT_NAME
        self.description = description
        self.model = model or AgentConfig.DEFAULT_MODEL
        self.client = get_openai_client(client)
        self.output_type = output_type
        self.system_prompt = system_prompt or ""

        # Lazy-import concrete Local implementations so that optional
        # heavy dependencies (chromadb, langfuse) are only loaded when the
        # caller does not provide their own storage backends.
        if message_storage is not None:
            self.message_storage = message_storage
        else:
            from ..components import MessageStorageLocal
            self.message_storage = MessageStorageLocal()

        if memory_storage is not None:
            self.memory_storage = memory_storage
        else:
            from ..components import MemoryStorageLocal
            self.memory_storage = MemoryStorageLocal(collection_name=self.name)

        self.logger = logging.getLogger(self.__class__.__name__)

        # Sub-components
        self._image_processor = ImageProcessor(
            self.client,
            caption_model=AgentConfig.IMAGE_CAPTION_MODEL,
            caption_prompt=AgentConfig.IMAGE_CAPTION_PROMPT,
            logger=self.logger,
        )
        self._tool_executor = ToolExecutor(self.logger, self._image_processor)
        self._mcp_manager = MCPManager(
            servers=self._parse_mcp_servers(mcp_servers),
            cache_ttl=AgentConfig.MCP_CACHE_TTL,
            logger=self.logger,
        )
        self._model_caller = ModelCaller(self.client, self.model, self.logger)

        # HTTP client pool for sub-agent tools
        self._http_clients: dict = {}

        # Background task throttle
        self._background_tasks: set = set()
        self._background_task_semaphore = asyncio.Semaphore(
            AgentConfig.DEFAULT_MAX_BACKGROUND_TASKS
        )

        # Register tools
        self._tool_executor.register(tools or [])
        agent_tools = self._convert_sub_agents_to_tools(sub_agents)
        if agent_tools:
            self._tool_executor.register(agent_tools)
            self.logger.info(
                "Registered agent tools: %s",
                [t.tool_spec["name"] for t in agent_tools],
            )

        # Shared-session identifiers
        self.SHARED_USER_ID = f"{self.name.upper()}_SHARED_USER"
        self.SHARED_SESSION_ID = f"{self.name.upper()}_SHARED_SESSION"
        self.SHARED_HISTORY_COUNT = 10

    # ------------------------------------------------------------------
    # Backward-compatible attribute proxies
    # ------------------------------------------------------------------

    @property
    def tools(self) -> dict:
        return self._tool_executor.tools

    @property
    def mcp_tools(self) -> dict:
        return self._tool_executor.mcp_tools

    @property
    def cached_tool_specs(self):
        return self._tool_executor.cached_specs

    @property
    def mcp_servers(self) -> List[str]:
        return self._mcp_manager.servers

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def normalize_session_id(self, session_id: str) -> str:
        """Return the storage session identifier scoped to this agent."""
        return _normalize_session_id(self.name, session_id)

    async def __call__(self, user_message: str, **kwargs):
        """Shortcut — delegates to :meth:`chat`."""
        return await self.chat(user_message=user_message, **kwargs)

    @observe()
    async def chat(
        self,
        user_message: str,
        user_id: str = AgentConfig.DEFAULT_USER_ID,
        session_id: str = AgentConfig.DEFAULT_SESSION_ID,
        history_count: int = AgentConfig.DEFAULT_HISTORY_COUNT,
        max_iter: int = AgentConfig.DEFAULT_MAX_ITER,
        max_concurrent_tools: int = AgentConfig.DEFAULT_MAX_CONCURRENT_TOOLS,
        image_source: Optional[Union[str, List[str]]] = None,
        output_type: Optional[type[BaseModel]] = None,
        stream: bool = False,
        enable_memory: bool = False,
        shared: bool = False,
    ) -> Union[str, BaseModel, AsyncGenerator[str, None]]:
        """Generate a reply from the agent given a user message."""
        session_id = self.normalize_session_id(session_id)
        if output_type is None:
            output_type = self.output_type
        if output_type:
            stream = False

        try:
            # Refresh MCP tools and sync to executor
            await self._mcp_manager.refresh()
            self._tool_executor.update_mcp_tools(
                self._mcp_manager.tools, self._mcp_manager.last_updated or 0.0
            )

            await self._store_user_message(user_message, user_id, session_id, image_source)
            input_messages = [
                msg.to_dict()
                for msg in await self.message_storage.get_messages(
                    user_id, session_id, history_count
                )
            ]
            messages_no_tool = [
                m for m in input_messages
                if m.get("role") in (RoleType.USER.value, RoleType.ASSISTANT.value)
            ]

            shared_context = None
            if shared:
                self.logger.info("Shared mode enabled for user_id: %s", user_id)
                await self._store_user_message(
                    f"{user_id} say: {user_message}",
                    self.SHARED_USER_ID, self.SHARED_SESSION_ID, image_source,
                )
                shared_msgs = [
                    msg.to_dict()
                    for msg in await self.message_storage.get_messages(
                        self.SHARED_USER_ID, self.SHARED_SESSION_ID,
                        self.SHARED_HISTORY_COUNT,
                    )
                ]
                shared_no_tool = [
                    m for m in shared_msgs
                    if m.get("role") in (RoleType.USER.value, RoleType.ASSISTANT.value)
                ]
                shared_memories = []
                if enable_memory:
                    pre = messages_no_tool[-3:]
                    shared_qp = self._should_preprocess_memory_query(
                        f"{user_id} say: {user_message}", pre
                    )
                    shared_memories = await self.memory_storage.retrieve(
                        user_id=self.SHARED_USER_ID,
                        query=f"{user_id} say: {user_message}",
                        limit=5,
                        query_context=f"pre_chat:{pre}",
                        enable_query_process=shared_qp,
                    )
                    self._schedule_memory_add(
                        self.SHARED_USER_ID, shared_no_tool[-2:], "shared memory sync"
                    )
                shared_context = (
                    f"shared_messages:{shared_no_tool} \n\n shared_memories:{shared_memories}"
                )

            retrieved_memories: list = []
            if enable_memory:
                pre = messages_no_tool[-3:]
                qp = self._should_preprocess_memory_query(user_message, pre)
                retrieved_memories = await self.memory_storage.retrieve(
                    user_id=user_id, query=user_message, limit=5,
                    query_context=f"pre_chat:{pre}", enable_query_process=qp,
                )
                self._schedule_memory_add(
                    user_id, messages_no_tool[-2:], "conversation memory sync"
                )

            for _ in range(max_iter):
                system_prompt_str = self._build_system_prompt(
                    user_id, retrieved_memories, shared_context
                )
                store_fn = lambda t: self._store_model_reply(t, user_id, session_id)
                shared_fn = (
                    (lambda t: self._store_model_reply(t, self.SHARED_USER_ID, self.SHARED_SESSION_ID))
                    if shared else None
                )
                reply_type, response = await self._model_caller.call(
                    input_messages, system_prompt_str,
                    self._tool_executor.cached_specs,
                    output_type, stream, store_fn, shared_fn,
                )

                if reply_type == ReplyType.SIMPLE_REPLY:
                    if not stream:
                        await self._store_model_reply(str(response), user_id, session_id)
                        if shared:
                            await self._store_model_reply(
                                str(response), self.SHARED_USER_ID, self.SHARED_SESSION_ID
                            )
                    return response
                if reply_type == ReplyType.STRUCTURED_REPLY:
                    await self._store_model_reply(response.model_dump_json(), user_id, session_id)
                    if shared:
                        await self._store_model_reply(
                            str(response), self.SHARED_USER_ID, self.SHARED_SESSION_ID
                        )
                    return response
                if reply_type == ReplyType.TOOL_CALL:
                    tool_result = await self._tool_executor.handle_calls(
                        response, self.message_storage,
                        user_id, session_id, input_messages, max_concurrent_tools,
                    )
                    if tool_result is not None:
                        image_data, description = tool_result
                        await self._store_model_reply(description, user_id, session_id)
                        if shared:
                            await self._store_model_reply(
                                description, self.SHARED_USER_ID, self.SHARED_SESSION_ID
                            )
                        return image_data
                else:
                    self.logger.error("Unknown reply type: %s", reply_type)
                    return "Sorry, I encountered an error while processing your request."

            self.logger.error(
                "Agent '%s' reached max_iter (%d). Consider increasing max_iter.",
                self.name, max_iter,
            )
            return (
                f"Agent '{self.name}' reached the maximum iteration limit ({max_iter}). "
                "This usually means a tool is in a loop or the model cannot determine "
                "how to finish the task. Consider increasing max_iter or simplifying the request."
            )

        except Exception as exc:
            self.logger.exception("Agent chat error: %s", exc)
            return "Sorry, something went wrong."

    def as_tool(
        self,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ):
        """Convert this agent into an OpenAI tool callable."""

        @function_tool(
            name=name or self.name,
            description=description or self.description,
            param_descriptions={
                "input": (
                    "A clear, focused instruction or question for the agent, "
                    "sufficient to complete the task independently."
                ),
                "expected_output": "Desired output format, structure, or content type.",
                "image_source": (
                    "Optional list of image URLs, file paths, or base64 strings."
                ),
            },
        )
        async def tool_func(
            input: str,
            expected_output: str,
            image_source: Optional[List[str]] = None,
        ):
            msg = f"### User Input:\n{input}"
            if expected_output:
                msg += f"\n\n### Expected Output:\n{expected_output}"
            return await self.chat(
                user_message=msg,
                image_source=image_source,
                user_id=f"agent_{self.name}_as_tool",
                session_id=str(uuid.uuid4()),
            )

        return tool_func

    # ------------------------------------------------------------------
    # Tool registration (backward-compatible method)
    # ------------------------------------------------------------------

    def _register_tools(self, tools: Optional[list]) -> None:
        """Delegate to ToolExecutor (kept for backward compatibility)."""
        self._tool_executor.register(tools or [])

    # ------------------------------------------------------------------
    # Sub-agent tool conversion
    # ------------------------------------------------------------------

    def _convert_sub_agents_to_tools(
        self,
        sub_agents: Optional[List],
    ) -> Optional[list]:
        tools = []
        for item in sub_agents or []:
            if isinstance(item, tuple) and len(item) == 3:
                name, description, server = item
                tools.append(
                    self._convert_http_agent_to_tool(server, name, description)
                )
            elif isinstance(item, Agent):
                tools.append(item.as_tool())
            else:
                self.logger.warning(
                    "Invalid sub_agent type: %s. Must be tuple or Agent.", type(item)
                )
        return tools or None

    def _convert_http_agent_to_tool(
        self,
        server: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ):
        """Wrap an HTTP agent server as an OpenAI tool."""
        return make_http_agent_tool(
            server=server,
            name=name,
            description=description,
            get_http_client_fn=lambda: self._get_http_client(server),
            retry_attempts=AgentConfig.RETRY_ATTEMPTS,
            retry_min_wait=AgentConfig.RETRY_MIN_WAIT,
            retry_max_wait=AgentConfig.RETRY_MAX_WAIT,
            error_preview_length=AgentConfig.ERROR_RESPONSE_PREVIEW_LENGTH,
        )

    # ------------------------------------------------------------------
    # Message storage helpers
    # ------------------------------------------------------------------

    async def _store_user_message(
        self,
        user_message: str,
        user_id: str,
        session_id: str,
        image_source: Optional[Union[str, List[str]]],
    ) -> None:
        detected = extract_image_urls_from_text(user_message)
        if detected:
            existing = (
                image_source if isinstance(image_source, list)
                else ([image_source] if image_source else [])
            )
            image_source = list(dict.fromkeys(existing + detected))
        msg = Message.create(
            content=user_message, role=RoleType.USER, image_source=image_source
        )
        await self.message_storage.add_messages(user_id, session_id, msg)

    async def _store_model_reply(
        self, reply_text: str, user_id: str, session_id: str
    ) -> None:
        msg = Message.create(content=reply_text, role=RoleType.ASSISTANT)
        await self.message_storage.add_messages(user_id, session_id, msg)

    # ------------------------------------------------------------------
    # System prompt
    # ------------------------------------------------------------------

    def _build_system_prompt(
        self,
        user_id: str,
        retrieved_memories: Optional[list] = None,
        shared_context: Optional[str] = None,
    ) -> str:
        sections = [
            AgentConfig.DEFAULT_SYSTEM_PROMPT.rstrip(),
            f"- Current user_id: {user_id}",
            f"- Current date: {time.strftime('%Y-%m-%d')}",
            f"- Current timezone: {time.tzname[0]}",
            "",
            f"- Retrieve relevant memories for user: {retrieved_memories or 'No relevant memories found.'}",
            "",
            f"- Shared context: {shared_context or 'No shared context.'}",
        ]
        if self.system_prompt:
            sections.extend(["", self.system_prompt])
        return "\n".join(sections)

    # ------------------------------------------------------------------
    # Memory helpers
    # ------------------------------------------------------------------

    def _should_preprocess_memory_query(
        self, query: str, pre_chat: Optional[list] = None
    ) -> bool:
        llm_service = getattr(self.memory_storage, "llm_service", None)
        if llm_service and hasattr(llm_service, "should_preprocess_query"):
            return llm_service.should_preprocess_query(query, pre_chat)
        return False

    def _schedule_memory_add(
        self, user_id: str, messages: list, description: str
    ) -> None:
        if not messages:
            return
        self._schedule_background_task(
            lambda: self.memory_storage.add(user_id=user_id, messages=messages),
            description=description,
        )

    # ------------------------------------------------------------------
    # Background task runner
    # ------------------------------------------------------------------

    def _schedule_background_task(
        self, task_factory: Callable[[], Awaitable[Any]], description: str
    ) -> None:
        task = asyncio.create_task(
            self._run_background_task(task_factory, description)
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_background_task(
        self, task_factory: Callable[[], Awaitable[Any]], description: str
    ) -> None:
        async with self._background_task_semaphore:
            last_error: Optional[Exception] = None
            for attempt in range(1, AgentConfig.BACKGROUND_TASK_ATTEMPTS + 1):
                try:
                    await task_factory()
                    return
                except Exception as exc:
                    last_error = exc
                    self.logger.warning(
                        "Background task failed (%s), attempt %d/%d: %s",
                        description, attempt, AgentConfig.BACKGROUND_TASK_ATTEMPTS, exc,
                    )
                    if attempt < AgentConfig.BACKGROUND_TASK_ATTEMPTS:
                        await asyncio.sleep(
                            AgentConfig.BACKGROUND_TASK_BASE_DELAY * attempt
                        )
            if last_error is not None:
                self.logger.error(
                    "Background task permanently failed (%s): %s",
                    description, last_error,
                )

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _get_http_client(self, server: str) -> httpx.AsyncClient:
        """Return a reused long-lived HTTP client for *server*."""
        base_url = server.rstrip("/")
        if base_url not in self._http_clients:
            self._http_clients[base_url] = httpx.AsyncClient(
                base_url=base_url, timeout=AgentConfig.HTTP_TIMEOUT
            )
        return self._http_clients[base_url]

    @staticmethod
    def _parse_mcp_servers(
        mcp_servers: Optional[Union[str, List[str]]]
    ) -> List[str]:
        if not mcp_servers:
            return []
        if isinstance(mcp_servers, str):
            return [mcp_servers]
        return list(mcp_servers)
