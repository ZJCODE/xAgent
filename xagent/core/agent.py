# Standard library imports
import asyncio
import json
import logging
import time
import uuid
from enum import Enum
from typing import Any, AsyncGenerator, Awaitable, Callable, List, Optional, Union

# Third-party imports
import httpx
from langfuse import observe
from langfuse.openai import AsyncOpenAI
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential

# Local imports
from ..components import MessageStorageBase, MessageStorageLocal, MemoryStorageBase, MemoryStorageLocal
from ..schemas import Message, ToolCall,RoleType, MessageType
from .session import normalize_session_id
from ..utils.tool_decorator import function_tool
from ..utils.image_utils import is_image_output, extract_source, extract_image_urls_from_text


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

class AgentConfig:
    """Configuration constants for Agent class."""
    
    DEFAULT_NAME = "default_agent"
    DEFAULT_MODEL = "gpt-4.1-mini"
    DEFAULT_USER_ID = "default_user"
    DEFAULT_SESSION_ID = "default_session"
    DEFAULT_HISTORY_COUNT = 16
    DEFAULT_MAX_ITER = 10
    DEFAULT_MAX_CONCURRENT_TOOLS = 10  # Maximum concurrent tool calls
    MCP_CACHE_TTL = 300  # 5 minutes
    HTTP_TIMEOUT = 600.0  # 10 minutes
    TOOL_RESULT_PREVIEW_LENGTH = 20
    ERROR_RESPONSE_PREVIEW_LENGTH = 200
    IMAGE_CAPTION_MODEL = "gpt-4o-mini"  # lightweight vision model for image captioning
    IMAGE_CAPTION_PROMPT = (
        "Describe this image in detail for future reference. Include: subject matter, "
        "composition, colors, style, mood, and any notable details. Be concise but thorough. "
        "Respond in the same language as the user's original prompt if provided."
    )
    
    # Retry configuration
    RETRY_ATTEMPTS = 3
    RETRY_MIN_WAIT = 1
    RETRY_MAX_WAIT = 60
    BACKGROUND_TASK_ATTEMPTS = 3
    BACKGROUND_TASK_BASE_DELAY = 0.5
    DEFAULT_MAX_BACKGROUND_TASKS = 4
    
    DEFAULT_SYSTEM_PROMPT = (
        "**Context Information:**\n"
    )


class ReplyType(Enum):
    """Types of replies the agent can generate."""
    
    SIMPLE_REPLY = "simple_reply"
    STRUCTURED_REPLY = "structured_reply"
    TOOL_CALL = "tool_call"
    ERROR = "error"


class Agent:
    """
    Base class for creating an AI agent that can interact with users, manage tools, and handle multi-step reasoning.
    
    This class provides a comprehensive framework for building AI agents with the following capabilities:
    - Multi-turn conversation handling
    - Tool integration and execution
    - MCP (Model Context Protocol) server support
    - Sub-agent delegation
    - Structured output generation
    - Streaming responses
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
        sub_agents: Optional[List[Union[tuple[str, str, str], 'Agent']]] = None,
        output_type: Optional[type[BaseModel]] = None,
        message_storage: Optional[MessageStorageBase] = None,
        memory_storage: Optional[MemoryStorageBase] = None,
    ):
        """
        Initialize the Agent with optional parameters.
        
        Args:
            name: The name of the agent
            system_prompt: Custom system prompt to prepend to the default
            description: Simple description of the agent for tool conversion
            model: The OpenAI model to use
            client: Custom OpenAI client instance
            tools: List of tool functions to register
            mcp_servers: MCP server URLs to fetch tools from
            sub_agents: List of sub-agents to convert to tools
            output_type: Pydantic model for structured output
            message_storage: MessageStorageBase instance for message storage
            memory_storage: MemoryStorageBase instance for long-term memory
        """
        # Basic configuration
        self.name = name or AgentConfig.DEFAULT_NAME
        self.description = description
        self.model = model or AgentConfig.DEFAULT_MODEL
        self.client = client or AsyncOpenAI()
        self.output_type = output_type
        
        # Message storage setup
        if message_storage is not None:
            self.message_storage = message_storage
        else:
            self.message_storage = MessageStorageLocal()
        
        # Memory setup
        if memory_storage is not None:
            self.memory_storage = memory_storage
        else:
            self.memory_storage = MemoryStorageLocal(collection_name = self.name)
        
        # System prompt setup
        self.system_prompt = system_prompt or ""
        
        # Tool management
        self.tools: dict = {}
        self.mcp_tools: dict = {}
        self.mcp_tools_last_updated: Optional[float] = None
        self.mcp_cache_ttl = AgentConfig.MCP_CACHE_TTL
        
        # Tool specs cache
        self._tool_specs_cache: Optional[list] = None
        self._tools_last_updated: Optional[float] = None
        
        # Initialize components
        self.logger = logging.getLogger(self.__class__.__name__)
        self.mcp_servers = self._normalize_mcp_servers(mcp_servers)
        self._http_clients: dict[str, httpx.AsyncClient] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._background_task_semaphore = asyncio.Semaphore(
            AgentConfig.DEFAULT_MAX_BACKGROUND_TASKS
        )
        
        # Register tools
        self._register_tools(tools or [])
        
        # Convert and register sub-agents as tools
        agent_tools = self._convert_sub_agents_to_tools(sub_agents)
        if agent_tools:
            self._register_tools(agent_tools)
            self.logger.info("Registered agent tools: %s", 
                           [tool.tool_spec['name'] for tool in agent_tools])
            
        # Shared
        self.SHARED_USER_ID = f"{self.name.upper()}_SHARED_USER"
        self.SHARED_SESSION_ID = f"{self.name.upper()}_SHARED_SESSION"
        self.SHARED_HISTORY_COUNT = 10

    def normalize_session_id(self, session_id: str) -> str:
        """Return the storage session identifier used by this agent."""
        return normalize_session_id(self.name, session_id)

    async def __call__(
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
        shared: bool = False
    ) -> Union[str, BaseModel, AsyncGenerator[str, None]]:
        """Call the agent to generate a reply based on the user message."""
        return await self.chat(
            user_message=user_message,
            user_id=user_id,
            session_id=session_id,
            history_count=history_count,
            max_iter=max_iter,
            max_concurrent_tools=max_concurrent_tools,
            image_source=image_source,
            output_type=output_type,
            stream=stream,
            enable_memory=enable_memory,
            shared=shared
        )

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
        shared: bool = False
    ) -> Union[str, BaseModel, AsyncGenerator[str, None]]:
        """
        Generate a reply from the agent given a user message.

        Args:
            user_message: The latest user message
            user_id: User identifier for message storage
            session_id: Session identifier for message storage
            history_count: Number of previous messages to include
            max_iter: Maximum model call attempts
            max_concurrent_tools: Maximum number of concurrent tool calls
            image_source: Source of the image, if any (URL, file path, or base64 string, or list of these)
            output_type: Pydantic model for structured output
            stream: Whether to stream the response
            enable_memory: Whether to enable memory storage and retrieval
            shared: Whether to enable the agent can share current chat with other user or agent (like in a group chat or collaboration or multi-user scenarios)

        Returns:
            The agent's reply, structured output, or error message
        """

        # Create agent-scoped session ID with consistent formatting
        session_id = self.normalize_session_id(session_id)

        if output_type is None:
            output_type = self.output_type
            
        if output_type:
            stream = False  # Structured output does not support streaming

        try:
            # Register tools and MCP servers in each chat call to make sure they are up-to-date
            await self._register_mcp_servers(self.mcp_servers)

            # Store the incoming user message in session history
            await self._store_user_message(user_message, user_id, session_id, image_source)

            # Build input messages once outside the loop
            input_messages = [msg.to_dict() for msg in await self.message_storage.get_messages(user_id, session_id, history_count)]
            messages_without_tool = [input for input in input_messages if input.get("role") in (RoleType.USER.value, RoleType.ASSISTANT.value)]

            shared_context = None
            if shared: # usually for group chat or collaboration or agent in an environment with multiple interactions
                self.logger.info("Shared mode enabled for user_id: %s", user_id) 
                await self._store_user_message(user_message = f"{user_id} say: {user_message}", 
                                               user_id = self.SHARED_USER_ID, session_id = self.SHARED_SESSION_ID, image_source=image_source)
                shared_messages = [msg.to_dict() for msg in await self.message_storage.get_messages(self.SHARED_USER_ID,
                                                                                                    self.SHARED_SESSION_ID,
                                                                                                    self.SHARED_HISTORY_COUNT)]
                shared_messages_without_tool = [input for input in shared_messages if input.get("role") in (RoleType.USER.value, RoleType.ASSISTANT.value)]
                shared_memories = []
                if enable_memory:
                    pre_chat = messages_without_tool[-3:] # Use last 4 messages for shared memory retrieval and memory storage
                    shared_query_process = self._should_preprocess_memory_query(
                        f"{user_id} say: {user_message}",
                        pre_chat,
                    )
                    shared_memories = await self.memory_storage.retrieve(user_id=self.SHARED_USER_ID,
                                                                            query= f"{user_id} say: {user_message}", limit=5, 
                                                                            query_context=f"pre_chat:{pre_chat}",enable_query_process=shared_query_process)
                    self._schedule_memory_add(
                        user_id=self.SHARED_USER_ID,
                        messages=shared_messages_without_tool[-2:],
                        description="shared memory sync",
                    )
                shared_context = f"shared_messages:{shared_messages_without_tool} \n\n shared_memories:{shared_memories}"

            retrieved_memories = []
            if enable_memory:
                pre_chat = messages_without_tool[-3:] # Use last 4 messages for memory retrieval memory and memory storage
                query_process = self._should_preprocess_memory_query(user_message, pre_chat)
                retrieved_memories = await self.memory_storage.retrieve(user_id=user_id, query=user_message, limit=5, 
                                                                        query_context=f"pre_chat:{pre_chat}",enable_query_process=query_process)
                
                self._schedule_memory_add(
                    user_id=user_id,
                    messages=messages_without_tool[-2:],
                    description="conversation memory sync",
                )

            for attempt in range(max_iter):

                reply_type, response = await self._call_model(input_messages, user_id, session_id, output_type, stream, retrieved_memories, shared_context)

                if reply_type == ReplyType.SIMPLE_REPLY:
                    if not stream:
                        await self._store_model_reply(str(response), user_id, session_id)
                        if shared:
                            await self._store_model_reply(str(response), user_id=self.SHARED_USER_ID, session_id=self.SHARED_SESSION_ID)
                    return response
                elif reply_type == ReplyType.STRUCTURED_REPLY:
                    await self._store_model_reply(response.model_dump_json(), user_id, session_id)
                    if shared:
                        await self._store_model_reply(str(response), user_id=self.SHARED_USER_ID, session_id=self.SHARED_SESSION_ID)
                    return response
                elif reply_type == ReplyType.TOOL_CALL:
                    tool_result = await self._handle_tool_calls(response, user_id, session_id, input_messages, max_concurrent_tools)
                    if tool_result is not None:
                        # Tool produced image(s) — store the descriptive summary
                        # in history (so the model retains context about what was
                        # generated), but return the real image data to the UI.
                        image_data, description = tool_result
                        await self._store_model_reply(description, user_id, session_id)
                        if shared:
                            await self._store_model_reply(description, user_id=self.SHARED_USER_ID, session_id=self.SHARED_SESSION_ID)
                        return image_data
                else:
                    self.logger.error("Unknown reply type: %s", reply_type)
                    return "Sorry, I encountered an error while processing your request."

            # If no valid reply after max_iter attempts
            self.logger.error("Failed to generate response after %d attempts", max_iter)
            return "Sorry, I could not generate a response after multiple attempts."

        except Exception as e:
            self.logger.exception("Agent chat error: %s", e)
            return "Sorry, something went wrong."

    def as_tool(
        self, 
        name: Optional[str] = None, 
        description: Optional[str] = None
    ):
        """
        Convert the agent into an OpenAI tool function.
        Args:
            name (str): The name of the tool function.
            description (str): A brief description of what the tool does.
        Returns:
            function: An asynchronous function that can be used as an OpenAI tool.
        """

        @function_tool(
            name=name or self.name,
            description=description or self.description,
            param_descriptions={
                "input": "A clear, focused instruction or question for the agent, sufficient to complete the task independently, with any necessary resources included.",
                "expected_output": "Specification of the desired output format, structure, or content type.",
                "image_source": "Optional list of image URLs, file paths, or base64 strings to be included in the message. If provided, these images will be used as context for the agent's response."
            }
        )
        async def tool_func(input: str, expected_output: str,image_source: Optional[List[str]] = None):
            user_message = f"### User Input:\n{input}"
            if expected_output:
                user_message += f"\n\n### Expected Output:\n{expected_output}"
            return await self.chat(user_message=user_message,
                                   image_source=image_source,
                                   user_id=f"agent_{self.name}_as_tool", 
                                   session_id=f"{uuid.uuid4()}"
                                   )

        return tool_func

    def _convert_sub_agents_to_tools(self, sub_agents: Optional[List[Union[tuple[str, str, str], 'Agent']]]) -> Optional[list]:
        """
        Convert sub-agents into OpenAI tool functions.
        Args:
            sub_agents (Optional[List[Union[tuple[str, str, str], 'Agent']]]): List of sub-agents to convert.
            Each item can be a tuple (name, description, server_url) or an Agent instance.
        Returns:
            Optional[list]: List of tool functions converted from sub-agents.
        """
        tools = []
        for item in sub_agents or []:
            if isinstance(item, tuple) and len(item) == 3:
                name, description, server = item
                tool = self._convert_http_agent_to_tool(server=server, name=name, description=description)
                tools.append(tool)
            elif isinstance(item, Agent):
                tool = item.as_tool()
                tools.append(tool)
            else:
                self.logger.warning(f"Invalid sub_agent type: {type(item)}. Must be tuple[name, description, server_url] or Agent instance.")
        return tools if tools else None

    def _register_tools(self, tools: Optional[list]) -> None:
        """
        Register tool functions with the agent.
        Ensures each tool function is asynchronous and unique.
        Args:
            tools (Optional[list]): List of tool functions to register.
        """
        for fn in tools or []:
            if not asyncio.iscoroutinefunction(fn):
                raise TypeError(f"Tool function '{fn.tool_spec['name']}' must be async.")
            if fn.tool_spec['name'] not in self.tools:
                self.tools[fn.tool_spec['name']] = fn
        
        # 注册新工具后使缓存失效
        self._tool_specs_cache = None

    @observe()
    @retry(
        stop=stop_after_attempt(AgentConfig.RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=AgentConfig.RETRY_MIN_WAIT, max=AgentConfig.RETRY_MAX_WAIT)
    )
    async def _register_mcp_servers(self, mcp_servers: Optional[Union[str, list]]) -> None:
        """
        Register tools from MCP servers, updating the local cache if needed.
        Args:
            mcp_servers (Optional[Union[str, list]]): MCP server URLs to fetch tools from.
        """
        now = time.time()
        if self.mcp_tools_last_updated and (now - self.mcp_tools_last_updated) < self.mcp_cache_ttl:
            return

        self.mcp_tools = {}
        if isinstance(mcp_servers, str):
            mcp_servers = [mcp_servers]
        for url in mcp_servers or []:
            try:
                from ..utils.mcp_convertor import MCPTool

                mt = MCPTool(url)
                mcp_tools = await mt.get_openai_tools()
                for tool in mcp_tools:
                    if tool.tool_spec['name'] not in self.mcp_tools:
                        self.mcp_tools[tool.tool_spec['name']] = tool
            except Exception as e:
                self.logger.error(f"Failed to get tools from MCP server {url}: {e}")
                # If one server fails, we should probably not update the timestamp
                # to try again on the next call. But for now, we continue with other servers.
                continue
        
        self.mcp_tools_last_updated = now

    async def _store_user_message(self, user_message: str, user_id: str, session_id: str, image_source: Optional[Union[str, List[str]]]) -> None:
        # Auto-detect image URLs / data URIs embedded in the message text
        # so the model receives them as proper vision inputs even when the
        # caller did not supply an explicit image_source.
        detected = extract_image_urls_from_text(user_message)
        if detected:
            existing = []
            if image_source:
                existing = image_source if isinstance(image_source, list) else [image_source]
            # Merge & deduplicate while preserving order
            merged = list(dict.fromkeys(existing + detected))
            image_source = merged

        msg = Message.create(content=user_message, role=RoleType.USER, image_source=image_source)
        await self.message_storage.add_messages(user_id, session_id, msg)

    async def _store_model_reply(self, reply_text: str, user_id: str, session_id: str) -> None:
        model_msg = Message.create(content=reply_text, role=RoleType.ASSISTANT)
        await self.message_storage.add_messages(user_id, session_id, model_msg)

    def _build_system_prompt(
        self,
        user_id: str,
        retrieved_memories: Optional[List[dict]] = None,
        shared_context: Optional[str] = None,
    ) -> str:
        """Build the runtime system prompt without formatting user-authored prompt text."""
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

    def _should_preprocess_memory_query(
        self,
        query: str,
        pre_chat: Optional[List[dict]] = None,
    ) -> bool:
        """Use heuristics by default to avoid unnecessary LLM query rewriting."""
        llm_service = getattr(self.memory_storage, "llm_service", None)
        if llm_service and hasattr(llm_service, "should_preprocess_query"):
            return llm_service.should_preprocess_query(query, pre_chat)
        return False

    def _schedule_memory_add(
        self,
        user_id: str,
        messages: List[dict],
        description: str,
    ) -> None:
        """Schedule memory writes on the controlled background runner."""
        if not messages:
            return
        self._schedule_background_task(
            lambda: self.memory_storage.add(user_id=user_id, messages=messages),
            description=description,
        )

    def _schedule_background_task(
        self,
        task_factory: Callable[[], Awaitable[Any]],
        description: str,
    ) -> None:
        task = asyncio.create_task(self._run_background_task(task_factory, description))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_background_task(
        self,
        task_factory: Callable[[], Awaitable[Any]],
        description: str,
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
                        description,
                        attempt,
                        AgentConfig.BACKGROUND_TASK_ATTEMPTS,
                        exc,
                    )
                    if attempt < AgentConfig.BACKGROUND_TASK_ATTEMPTS:
                        await asyncio.sleep(
                            AgentConfig.BACKGROUND_TASK_BASE_DELAY * attempt
                        )

            if last_error is not None:
                self.logger.error(
                    "Background task permanently failed (%s): %s",
                    description,
                    last_error,
                )

    def _get_http_client(self, server: str) -> httpx.AsyncClient:
        """Reuse long-lived HTTP clients for sub-agent tools."""
        base_url = server.rstrip("/")
        client = self._http_clients.get(base_url)
        if client is None:
            client = httpx.AsyncClient(base_url=base_url, timeout=AgentConfig.HTTP_TIMEOUT)
            self._http_clients[base_url] = client
        return client

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

        response = getattr(event, "response", None)
        if response is not None:
            if getattr(response, "output_text", ""):
                return ReplyType.SIMPLE_REPLY
            output = getattr(response, "output", None) or []
            if any(getattr(output_item, "type", None) == "function_call" for output_item in output):
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

    @observe()
    @retry(
        stop=stop_after_attempt(AgentConfig.RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=AgentConfig.RETRY_MIN_WAIT, max=AgentConfig.RETRY_MAX_WAIT)
    )
    async def _call_model(self, input_msgs: list, 
                          user_id: str, session_id: str,
                          output_type: type[BaseModel] = None, 
                          stream: bool = False,
                          retrieved_memories: Optional[List[dict]] = None,
                          shared_context: Optional[str] = None
                          ) -> tuple[ReplyType, object]:
        """
        Call the AI model with the provided input messages.
        Args:
            input_msgs (list): List of input messages to send to the model.
            user_id (str): User identifier for message storage.
            session_id (str): Session identifier for message storage.
            output_type (type[BaseModel], optional): Pydantic model for structured output.
            stream (bool, optional): Whether to stream the response. Defaults to False.
            retrieved_memories: Optional[List[dict]], optional: Retrieved memories to include in the request.
            shared_context: Optional[str], optional: Shared context to include in the request.
        Returns:
            tuple[ReplyType, object]: A tuple containing the reply type and the response object.
        """
        system_msg = {
            "role": "system",
            "content": self._build_system_prompt(
                user_id=user_id,
                retrieved_memories=retrieved_memories,
                shared_context=shared_context,
            )
        }

        # 使用智能缓存的工具规格
        tool_specs = self.cached_tool_specs
        
        # 预处理消息
        messages = [system_msg] + self._sanitize_input_messages(input_msgs)

        try:
            # 根据是否需要结构化输出选择不同的API调用,结构化输出强制不使用Stream 模式
            if output_type is not None:
                response = await self.client.responses.parse(
                    model=self.model,
                    tools=tool_specs if tool_specs else [],
                    input=messages,
                    text_format=output_type
                )
                # 检查结构化输出
                if hasattr(response, "output_parsed") and response.output_parsed is not None:
                    return ReplyType.STRUCTURED_REPLY, response.output_parsed
            else:
                response = await self.client.responses.create(
                    model=self.model,
                    tools=tool_specs or [],
                    input=messages,
                    stream=stream
                )

            # 统一处理响应，按优先级检查不同类型的输出
            if not stream:
                if hasattr(response, 'output_text') and response.output_text:
                    return ReplyType.SIMPLE_REPLY, response.output_text
                
                if hasattr(response, 'output') and response.output:
                    return ReplyType.TOOL_CALL, response.output
                
                # 如果没有有效输出，记录警告并返回错误
                self.logger.warning("Model response contains no valid output: %s", response)
                return ReplyType.ERROR, "No valid output from model response."
            else:
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

                        for event in prefix_events:
                            event_response = getattr(event, "response", None)
                            if event_response is not None:
                                last_response = event_response
                            content = self._extract_stream_text_delta(event)
                            if content:
                                text_parts.append(content)
                                yield content

                        async for event in response:
                            event_response = getattr(event, "response", None)
                            if event_response is not None:
                                last_response = event_response
                            content = self._extract_stream_text_delta(event)
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
                            await self._store_model_reply(final_text, user_id, session_id)
                            if shared_context:
                                await self._store_model_reply(
                                    final_text,
                                    user_id=self.SHARED_USER_ID,
                                    session_id=self.SHARED_SESSION_ID,
                                )

                    return ReplyType.SIMPLE_REPLY, stream_generator()

                if stream_kind == ReplyType.TOOL_CALL:
                    async for event in response:
                        event_response = getattr(event, "response", None)
                        if event_response is not None:
                            last_response = event_response

                    tool_output = getattr(last_response, "output", None) or []
                    if tool_output:
                        return ReplyType.TOOL_CALL, tool_output

                    self.logger.warning("Stream response ended without tool output")
                    return ReplyType.ERROR, "No tool output from model response."

                final_text = self._extract_response_text(last_response)
                if final_text:
                    async def stream_generator():
                        yield final_text
                    return ReplyType.SIMPLE_REPLY, stream_generator()

                self.logger.warning("Stream response contains no recognized output")
                async def stream_generator():
                    yield "No valid output from model response."
                return ReplyType.ERROR, stream_generator()
                    
        except Exception as e:
            self.logger.exception("Model call failed: %s", e)
            if stream:
                error_message = f"Model call error: {str(e)}"
                async def stream_generator():
                    yield error_message
                return ReplyType.ERROR, stream_generator()
            return ReplyType.ERROR, f"Model call error: {str(e)}"
    
    @observe()
    async def _handle_tool_calls(self, tool_calls: list, user_id: str, session_id: str, input_messages: list, max_concurrent_tools: int = AgentConfig.DEFAULT_MAX_CONCURRENT_TOOLS) -> None:
        """
        Handle tool calls by executing them concurrently with concurrency limit.
        Args:
            tool_calls (list): List of tool call messages to process.
            user_id (str): User identifier for message storage.
            session_id (str): Session identifier for message storage.
            input_messages (list): List of input messages to update with tool call results.
            max_concurrent_tools (int): Maximum number of concurrent tool calls.
        Returns:
            None: This method modifies input_messages in place and does not return a value.
        """

        if tool_calls is None or not tool_calls:
            return None

        # Filter function calls only
        function_calls = [tc for tc in tool_calls if getattr(tc, "type", None) == "function_call"]
        
        if not function_calls:
            return None

        # Create semaphore to limit concurrent tool calls
        semaphore = asyncio.Semaphore(max_concurrent_tools)
        
        async def execute_with_semaphore(tool_call):
            async with semaphore:
                return await self._act(tool_call, user_id, session_id)

        # Execute tool calls with concurrency limit
        tasks = [execute_with_semaphore(tc) for tc in function_calls]
        results = await asyncio.gather(*tasks)
        
        # Collect any generated images and their descriptions from tool results
        pending_images = []
        pending_descriptions = []

        # Safely add all tool messages after concurrent execution
        for tool_messages, image_data, description in results:
            if tool_messages:
                input_messages.extend([msg.to_dict() for msg in tool_messages])
            if image_data:
                pending_images.append(image_data)
            if description:
                pending_descriptions.append(description)

        # If any tool produced images, return them directly to the user
        # instead of sending the huge base64 back to the model.
        if pending_images:
            return (
                "\n\n".join(pending_images),       # image data for the UI
                "\n\n".join(pending_descriptions),  # compact text for history
            )

        return None

    @staticmethod
    def _is_image_output(text: str) -> bool:
        """Check if a string represents an image (URL, data URI, or markdown image)."""
        return is_image_output(text)

    @staticmethod
    def _extract_image_source(text: str) -> str:
        """Extract the raw image URL or data URI, unwrapping markdown if present."""
        return extract_source(text)

    async def _caption_image(self, image_data_uri: str, prompt_hint: str = "") -> str:
        """
        Use a vision model to generate a detailed description of an image.
        This captures visual details that the generation prompt alone cannot convey.

        Args:
            image_data_uri: A data:image/... URI string.
            prompt_hint: The original generation prompt, used as context.

        Returns:
            A text description of the image, or a fallback if captioning fails.
        """
        caption_prompt = AgentConfig.IMAGE_CAPTION_PROMPT
        if prompt_hint:
            caption_prompt += f"\n\nOriginal generation prompt: \"{prompt_hint}\""

        try:
            response = await self.client.responses.create(
                model=AgentConfig.IMAGE_CAPTION_MODEL,
                input=[{
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": caption_prompt},
                        {"type": "input_image", "image_url": image_data_uri},
                    ]
                }],
            )
            caption = getattr(response, "output_text", "") or ""
            if caption.strip():
                return caption.strip()
        except Exception as e:
            self.logger.warning("Image captioning failed, falling back to prompt-based description: %s", e)

        # Fallback: use the generation prompt if captioning fails
        if prompt_hint:
            return f"Generated image based on prompt: \"{prompt_hint}\""
        return "An image was generated and displayed to the user."

    async def _act(self, tool_call, user_id: str, session_id: str) -> tuple[Optional[list], Optional[str], Optional[str]]:
        """
        Execute a single tool call and return the messages generated by the tool.
        Args:
            tool_call: The tool call message to process.
            user_id (str): User identifier for message storage.
            session_id (str): Session identifier for message storage.
        Returns:
            tuple: (messages_list, image_data_or_none, description_or_none)
                - messages_list: A list of messages generated by the tool call, or None.
                - image_data: The raw image markdown if the tool produced an image, else None.
                - description: A compact text description of the generated image, else None.
        """
        name = getattr(tool_call, "name", None)
        try:
            args = json.loads(getattr(tool_call, "arguments", "{}"))
        except Exception as e:
            self.logger.error("Tool args parse error: %s", e)
            return None, None, None
        func = self.tools.get(name) or self.mcp_tools.get(name)
        if func:
            self.logger.info("Calling tool: %s with args: %s", name, args)

            try:
                result = await func(**args)
            except Exception as e:
                self.logger.error("Tool call error: %s", e)
                result = f"Tool error: {e}"

            result_str = json.dumps(result, ensure_ascii=False) if isinstance(result, (dict, list)) else str(result)

            # Detect image results (URL or base64) — keep image data for
            # direct delivery to the user, but store a compact description
            # for the model so it retains context about what was generated
            # (enabling follow-up like "convert the previous image …").
            image_data = None
            model_output = result_str
            if self._is_image_output(result_str):
                image_data = result_str
                # Extract raw URL / data URI and caption via vision model
                image_src = self._extract_image_source(result_str)
                prompt_hint = args.get("prompt", "")
                caption = await self._caption_image(image_src, prompt_hint)
                model_output = (
                    f"[Image generated by tool `{name}` and displayed to user. "
                    f"Image description: {caption}]"
                )

            tool_call_msg = Message(
                type=MessageType.FUNCTION_CALL,
                role=RoleType.TOOL,
                content=f"Calling tool: `{name}` with args: {args}",
                tool_call=ToolCall(
                    call_id=getattr(tool_call, "call_id", ""),
                    name=name,
                    arguments=json.dumps(args, ensure_ascii=False)
                )
            )

            def _format_tool_result_preview(result: str) -> str:
                """Format tool result for preview in content."""
                result_str = str(result)
                if len(result_str) <= AgentConfig.TOOL_RESULT_PREVIEW_LENGTH:
                    return result_str
                return result_str[:AgentConfig.TOOL_RESULT_PREVIEW_LENGTH] + '...'

            tool_res_msg = Message(
                type=MessageType.FUNCTION_CALL_OUTPUT,
                role=RoleType.TOOL,
                content=f"Tool `{name}` result: {_format_tool_result_preview(result)}",
                tool_call=ToolCall(
                    call_id=getattr(tool_call, "call_id", "001"),
                    output=model_output
                )
            )
            await self.message_storage.add_messages(user_id, session_id, [tool_call_msg, tool_res_msg])
            
            return [tool_call_msg, tool_res_msg], image_data, model_output if image_data else None

        return None, None, None

    @staticmethod
    def _sanitize_input_messages(input_messages: list) -> list:
        """
        Checks if the first message is a function call output and removes it if so.
        Args:
            input_messages (list): List of input messages to sanitize.
        Returns:
            list: Sanitized list of input messages with no leading function call outputs.
        """
        while input_messages and input_messages[0].get("type") == MessageType.FUNCTION_CALL_OUTPUT:
            input_messages.pop(0)
        return input_messages
    

    def _normalize_mcp_servers(self, mcp_servers: Optional[Union[str, List[str]]]) -> List[str]:
        """Normalize MCP servers input to a list."""
        if not mcp_servers:
            return []
        if isinstance(mcp_servers, str):
            return [mcp_servers]
        return list(mcp_servers)

    @property
    def cached_tool_specs(self):
        """
        Returns the cached tool specifications, rebuilding if necessary.
        """
        if self._should_rebuild_cache():
            self._rebuild_tool_cache()
        return self._tool_specs_cache

    def _should_rebuild_cache(self) -> bool:
        """
        Determine if the tool specs cache should be rebuilt.
        Returns:
        - True if the cache is empty or tools have changed
        - False if the cache is valid
        """
        # If the cache is empty, we need to rebuild
        if self._tool_specs_cache is None:
            return True

        # If the MCP tools have been updated, we need to rebuild
        if self.mcp_tools_last_updated and (
            self._tools_last_updated is None or 
            self.mcp_tools_last_updated > self._tools_last_updated
        ):
            return True
        
        return False

    def _rebuild_tool_cache(self):
        """
        Rebuild the tool specs cache.
        This method collects all tools from both local and MCP sources,
        and updates the cache with their specifications.
        """
        all_tools = list(self.tools.values()) + list(self.mcp_tools.values())
        self._tool_specs_cache = [fn.tool_spec for fn in all_tools] if all_tools else None
        self._tools_last_updated = time.time()


    def _convert_http_agent_to_tool(
        self, 
        server: str, 
        name: Optional[str] = None, 
        description: Optional[str] = None
    ):
        """
        Convert an HTTP-based agent into an OpenAI tool function.
        Args:
            server (str): The base URL of the HTTP agent server.
            name (str): The name of the tool function.
            description (str): A brief description of what the tool does.
        Returns:
            function: An asynchronous function that can be used as an OpenAI tool.
        """
        
        @function_tool(
            name=name,
            description=description,
            param_descriptions={
                "input": "A clear, focused instruction or question for the agent, sufficient to complete the task independently, with any necessary resources included.",
                "expected_output": "Specification of the desired output format, structure, or content type.",
                "image_source": "Optional list of image URLs, file paths, or base64 strings to be included in the message. If provided, these images will be used as context for the agent's response."
            }
        )
        async def tool_func(input: str, expected_output: str, image_source: Optional[List[str]] = None):
            # 构建消息和请求体
            user_message = f"### User Input:\n{input}"
            if expected_output:
                user_message += f"\n\n### Expected Output:\n{expected_output}"
            
            payload = {
                "user_id": f"http_agent_tool_{name or 'default'}_{uuid.uuid4().hex[:8]}",
                "session_id": f"session_{uuid.uuid4().hex[:8]}",
                "user_message": user_message,
                "stream": False
            }
            
            if image_source:
                payload["image_source"] = image_source
            
            # 带重试的HTTP请求函数
            @retry(
                stop=stop_after_attempt(AgentConfig.RETRY_ATTEMPTS),
                wait=wait_exponential(multiplier=1, min=AgentConfig.RETRY_MIN_WAIT, max=AgentConfig.RETRY_MAX_WAIT)
            )
            async def make_http_request():
                client = self._get_http_client(server)
                return await client.post("/chat", json=payload)
            
            # 发送请求并处理响应
            try:
                response = await make_http_request()
                
                # 成功响应处理
                if response.status_code == 200:
                    data = response.json()
                    reply = data.get("reply", "")
                    if reply:
                        return reply
                    return "Empty reply from HTTP Agent"
                
                # 错误响应处理
                if response.status_code == 500:
                    try:
                        error_detail = response.json().get("detail", "Internal server error")
                        return f"HTTP Agent internal error: {error_detail}"
                    except:
                        return f"HTTP Agent internal error: {response.text[:AgentConfig.ERROR_RESPONSE_PREVIEW_LENGTH]}"
                
                return f"HTTP Agent error {response.status_code}: {response.text[:AgentConfig.ERROR_RESPONSE_PREVIEW_LENGTH]}"
                
            except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError) as e:
                error_type = type(e).__name__.replace('Exception', '').replace('Error', '')
                return f"HTTP Agent {error_type.lower()}: {str(e)}"
            except json.JSONDecodeError as e:
                return f"Invalid JSON response: {str(e)}"
            except Exception as e:
                self.logger.exception(f"HTTP Agent call failed: {e}")
                return f"Unexpected error: {str(e)}"

        return tool_func
