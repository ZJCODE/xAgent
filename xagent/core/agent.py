import logging
from pathlib import Path
from typing import AsyncGenerator, List, Optional, Union

from openai import AsyncOpenAI
from pydantic import BaseModel

from ..components import MessageStorageBase, MessageStorageLocal, MemoryStorageBase, MemoryStorageLocal
from ..schemas import RoleType
from .config import AgentConfig, ReplyType
from .session import normalize_session_id
from .tools import ToolManager, ToolExecutor, agent_as_tool, convert_sub_agents
from .handlers import ModelClient, MemoryManager, MessageHandler


logger = logging.getLogger(__name__)


class Agent:
    """
    AI agent that composes specialized components for conversation, tool use, and memory.

    Public API is unchanged:
    - ``Agent(name, system_prompt, model, tools, mcp_servers, sub_agents, ...)``
    - ``agent.chat(user_message, ...)`` / ``await agent(user_message, ...)``
    - ``agent.as_tool(name, description)``
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
        workspace: Optional[str] = None,
    ):
        # Basic configuration
        self.name = name or AgentConfig.DEFAULT_NAME
        self.description = description
        self.model = model or AgentConfig.DEFAULT_MODEL
        self.client = client or AsyncOpenAI()
        self.output_type = output_type
        self.system_prompt = system_prompt or ""

        # Resolve workspace for default local storage paths
        workspace_path: Optional[Path] = None
        if workspace is not None:
            workspace_path = Path(workspace).expanduser().resolve()

        # Message storage
        if message_storage is not None:
            self.message_storage = message_storage
        elif workspace_path is not None:
            msg_path = str(workspace_path / "messages.sqlite3")
            self.message_storage = MessageStorageLocal(path=msg_path)
        else:
            self.message_storage = MessageStorageLocal()

        # Memory storage
        if memory_storage is not None:
            self.memory_storage = memory_storage
        elif workspace_path is not None:
            chroma_path = str(workspace_path / "chroma")
            self.memory_storage = MemoryStorageLocal(path=chroma_path, collection_name=self.name)
        else:
            self.memory_storage = MemoryStorageLocal(collection_name=self.name)

        # --- Compose internal components ---
        self.tool_manager = ToolManager(tools=tools, mcp_servers=mcp_servers)
        self.model_client = ModelClient(client=self.client, model=self.model)
        self.memory_manager = MemoryManager(
            memory_storage=self.memory_storage,
            message_storage=self.message_storage,
        )
        self.message_handler = MessageHandler(
            message_storage=self.message_storage,
            system_prompt=self.system_prompt,
        )
        self.tool_executor = ToolExecutor(
            tool_manager=self.tool_manager,
            message_storage=self.message_storage,
            client=self.client,
        )

        # Register sub-agents as tools
        agent_tools = convert_sub_agents(sub_agents)
        if agent_tools:
            self.tool_manager.register_tools(agent_tools)
            logger.info("Registered agent tools: %s",
                        [tool.tool_spec['name'] for tool in agent_tools])

        # Shared-mode constants
        self.SHARED_USER_ID = f"{self.name.upper()}_SHARED_USER"
        self.SHARED_SESSION_ID = f"{self.name.upper()}_SHARED_SESSION"
        self.SHARED_HISTORY_COUNT = 10

    # ---- Backward-compatible property proxies ----

    @property
    def tools(self) -> dict:
        return self.tool_manager.tools

    @property
    def mcp_tools(self) -> dict:
        return self.tool_manager.mcp_tools

    def normalize_session_id(self, session_id: str) -> str:
        return normalize_session_id(self.name, session_id)

    # ---- Public API ----

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
        shared: bool = False,
    ) -> Union[str, BaseModel, AsyncGenerator[str, None]]:
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
            shared=shared,
        )

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
            # Ensure MCP tools are up-to-date
            await self.tool_manager.register_mcp_servers(self.tool_manager.mcp_servers)

            # Store user message
            await self.message_handler.store_user_message(user_message, user_id, session_id, image_source)

            # Build input messages
            input_messages = await self.message_handler.get_input_messages(user_id, session_id, history_count)
            messages_without_tool = self.message_handler.filter_non_tool_messages(input_messages)

            # Shared context
            shared_context = None
            if shared:
                shared_context = await self._build_shared_context(
                    user_message, user_id, session_id, image_source,
                    messages_without_tool, enable_memory,
                )

            # Memory retrieval
            retrieved_memories: list = []
            if enable_memory:
                pre_chat = messages_without_tool[-3:]
                retrieved_memories = await self.memory_manager.retrieve_memories(
                    user_id=user_id, query=user_message, pre_chat=pre_chat,
                )
                self.memory_manager.schedule_memory_add(
                    user_id=user_id, session_id=session_id,
                    messages=messages_without_tool[-2:],
                    description="conversation memory sync",
                )

            # Build system message
            system_msg = {
                "role": "system",
                "content": self.message_handler.build_system_prompt(
                    user_id=user_id,
                    retrieved_memories=retrieved_memories,
                    shared_context=shared_context,
                ),
            }
            model_messages = [system_msg] + self.message_handler.sanitize_input_messages(input_messages)

            # Agentic loop
            for _ in range(max_iter):
                reply_type, response = await self.model_client.call(
                    messages=model_messages,
                    tool_specs=self.tool_manager.cached_tool_specs,
                    output_type=output_type,
                    stream=stream,
                    store_reply=lambda text: self.message_handler.store_model_reply(text, user_id, session_id),
                    shared_store_reply=(
                        (lambda text: self.message_handler.store_model_reply(text, self.SHARED_USER_ID, self.SHARED_SESSION_ID))
                        if shared else None
                    ),
                )

                if reply_type == ReplyType.SIMPLE_REPLY:
                    if not stream:
                        await self.message_handler.store_model_reply(str(response), user_id, session_id)
                        if shared:
                            await self.message_handler.store_model_reply(
                                str(response), self.SHARED_USER_ID, self.SHARED_SESSION_ID
                            )
                    return response

                elif reply_type == ReplyType.STRUCTURED_REPLY:
                    await self.message_handler.store_model_reply(response.model_dump_json(), user_id, session_id)
                    if shared:
                        await self.message_handler.store_model_reply(
                            str(response), self.SHARED_USER_ID, self.SHARED_SESSION_ID
                        )
                    return response

                elif reply_type == ReplyType.TOOL_CALL:
                    tool_result = await self.tool_executor.handle_tool_calls(
                        response, user_id, session_id, input_messages, max_concurrent_tools,
                    )
                    if tool_result is not None:
                        image_data, description = tool_result
                        await self.message_handler.store_model_reply(description, user_id, session_id)
                        if shared:
                            await self.message_handler.store_model_reply(
                                description, self.SHARED_USER_ID, self.SHARED_SESSION_ID
                            )
                        return image_data
                    # Rebuild model messages with tool results for next iteration
                    model_messages = [system_msg] + self.message_handler.sanitize_input_messages(input_messages)

                else:
                    logger.error("Unknown reply type: %s", reply_type)
                    return "Sorry, I encountered an error while processing your request."

            logger.error("Failed to generate response after %d attempts", max_iter)
            return "Sorry, I could not generate a response after multiple attempts."

        except Exception as e:
            logger.exception("Agent chat error: %s", e)
            return "Sorry, something went wrong."

    def as_tool(self, name: Optional[str] = None, description: Optional[str] = None):
        """Convert this agent into an OpenAI tool function."""
        return agent_as_tool(self, name=name, description=description)

    # ---- Private helpers ----

    async def _build_shared_context(
        self,
        user_message: str,
        user_id: str,
        session_id: str,
        image_source,
        messages_without_tool: list,
        enable_memory: bool,
    ) -> str:
        """Build the shared context string for multi-user / group-chat scenarios."""
        logger.info("Shared mode enabled for user_id: %s", user_id)
        await self.message_handler.store_user_message(
            user_message=f"{user_id} say: {user_message}",
            user_id=self.SHARED_USER_ID,
            session_id=self.SHARED_SESSION_ID,
            image_source=image_source,
        )
        shared_input = await self.message_handler.get_input_messages(
            self.SHARED_USER_ID, self.SHARED_SESSION_ID, self.SHARED_HISTORY_COUNT,
        )
        shared_messages_without_tool = self.message_handler.filter_non_tool_messages(shared_input)

        shared_memories: list = []
        if enable_memory:
            pre_chat = messages_without_tool[-3:]
            shared_memories = await self.memory_manager.retrieve_memories(
                user_id=self.SHARED_USER_ID,
                query=f"{user_id} say: {user_message}",
                pre_chat=pre_chat,
            )
            self.memory_manager.schedule_memory_add(
                user_id=self.SHARED_USER_ID,
                session_id=self.SHARED_SESSION_ID,
                messages=shared_messages_without_tool[-2:],
                description="shared memory sync",
            )

        return f"shared_messages:{shared_messages_without_tool} \n\n shared_memories:{shared_memories}"
