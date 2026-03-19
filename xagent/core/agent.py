import logging
from pathlib import Path
from typing import AsyncGenerator, List, Optional, Union

from openai import AsyncOpenAI
from pydantic import BaseModel

from ..components import MessageStorageBase, MessageStorageLocal, MemoryStorageBase, MemoryStorageLocal
from .config import AgentConfig, ReplyType
from .handlers import MemoryManager, MessageHandler, ModelClient
from .tools import ToolExecutor, ToolManager
from ..tools import create_search_journal_memory_tool


logger = logging.getLogger(__name__)


def _normalize_agent_identifier(name: str) -> str:
    return (name or AgentConfig.DEFAULT_NAME).lower().replace(" ", "_").replace("-", "_")


class Agent:
    """AI agent runtime for a continuous agent-level message stream."""

    def __init__(
        self,
        name: Optional[str] = None,
        system_prompt: Optional[str] = None,
        description: Optional[str] = None,
        model: Optional[str] = None,
        client: Optional[AsyncOpenAI] = None,
        tools: Optional[List] = None,
        mcp_servers: Optional[Union[str, List[str]]] = None,
        output_type: Optional[type[BaseModel]] = None,
        message_storage: Optional[MessageStorageBase] = None,
        memory_storage: Optional[MemoryStorageBase] = None,
        workspace: Optional[str] = None,
    ):
        self.name = name or AgentConfig.DEFAULT_NAME
        self.description = description
        self.model = model or AgentConfig.DEFAULT_MODEL
        self.client = client or AsyncOpenAI()
        self.output_type = output_type
        self.system_prompt = system_prompt or ""
        self._assistant_sender_id = f"agent:{self.name}"
        self._agent_memory_key = f"agent:{self.name}"
        self._memory_tools_enabled = True

        workspace_path: Optional[Path] = None
        if workspace is not None:
            workspace_path = Path(workspace).expanduser().resolve()

        if message_storage is not None:
            self.message_storage = message_storage
        elif workspace_path is not None:
            self.message_storage = MessageStorageLocal(
                path=str(workspace_path / f"{_normalize_agent_identifier(self.name)}_messages.sqlite3")
            )
        else:
            default_workspace = Path(AgentConfig.DEFAULT_WORKSPACE).expanduser().resolve()
            default_workspace.mkdir(parents=True, exist_ok=True)
            self.message_storage = MessageStorageLocal(
                path=str(default_workspace / f"{_normalize_agent_identifier(self.name)}_messages.sqlite3")
            )

        if memory_storage is not None:
            self.memory_storage = memory_storage
        else:
            local_memory_path = getattr(self.message_storage, "path", None)
            if local_memory_path is None:
                raise ValueError(
                    "Default journal memory requires a SQLite-backed message storage path. "
                    "Provide MemoryStorageLocal(path=...) when using a custom message backend."
                )
            self.memory_storage = MemoryStorageLocal(
                path=str(local_memory_path),
            )

        self.memory_manager = MemoryManager(
            memory_storage=self.memory_storage,
            message_storage=self.message_storage,
        )
        bound_tools = list(tools or [])
        bound_tools.append(
            create_search_journal_memory_tool(
                memory_manager=self.memory_manager,
                memory_key=self.memory_key,
                is_enabled=lambda: self._memory_tools_enabled,
            )
        )
        self.tool_manager = ToolManager(tools=bound_tools, mcp_servers=mcp_servers)
        self.model_client = ModelClient(client=self.client, model=self.model)
        self.message_handler = MessageHandler(
            message_storage=self.message_storage,
            system_prompt=self.system_prompt,
        )
        self.tool_executor = ToolExecutor(
            tool_manager=self.tool_manager,
            message_storage=self.message_storage,
            client=self.client,
        )

    @property
    def tools(self) -> dict:
        return self.tool_manager.tools

    @property
    def mcp_tools(self) -> dict:
        return self.tool_manager.mcp_tools

    @property
    def memory_key(self) -> str:
        return self._agent_memory_key

    async def __call__(
        self,
        user_message: str,
        user_id: str = AgentConfig.DEFAULT_USER_ID,
        history_count: int = AgentConfig.DEFAULT_HISTORY_COUNT,
        max_iter: int = AgentConfig.DEFAULT_MAX_ITER,
        max_concurrent_tools: int = AgentConfig.DEFAULT_MAX_CONCURRENT_TOOLS,
        image_source: Optional[Union[str, List[str]]] = None,
        output_type: Optional[type[BaseModel]] = None,
        stream: bool = False,
        enable_memory: bool = True,
    ) -> Union[str, BaseModel, AsyncGenerator[str, None]]:
        return await self.chat(
            user_message=user_message,
            user_id=user_id,
            history_count=history_count,
            max_iter=max_iter,
            max_concurrent_tools=max_concurrent_tools,
            image_source=image_source,
            output_type=output_type,
            stream=stream,
            enable_memory=enable_memory,
        )

    async def chat(
        self,
        user_message: str,
        user_id: str = AgentConfig.DEFAULT_USER_ID,
        history_count: int = AgentConfig.DEFAULT_HISTORY_COUNT,
        max_iter: int = AgentConfig.DEFAULT_MAX_ITER,
        max_concurrent_tools: int = AgentConfig.DEFAULT_MAX_CONCURRENT_TOOLS,
        image_source: Optional[Union[str, List[str]]] = None,
        output_type: Optional[type[BaseModel]] = None,
        stream: bool = False,
        enable_memory: bool = True,
    ) -> Union[str, BaseModel, AsyncGenerator[str, None]]:
        """Generate a reply from the agent given a user message."""
        if output_type is None:
            output_type = self.output_type
        if output_type:
            stream = False

        try:
            await self.tool_manager.ensure_mcp_ready()
            self._memory_tools_enabled = enable_memory

            await self.message_handler.store_user_message(
                user_message,
                user_id,
                image_source,
            )

            recent_messages = await self.message_handler.get_recent_messages(
                history_count=history_count,
            )
            input_messages = self.message_handler.to_model_input(recent_messages)
            messages_without_tool = self.message_handler.filter_non_tool_messages(input_messages)

            retrieved_memories: list = []
            if enable_memory:
                retrieved_memories = await self.memory_manager.retrieve_context_memories(memory_key=self.memory_key)
                self.memory_manager.schedule_memory_add(
                    memory_key=self.memory_key,
                    messages=messages_without_tool[-2:],
                )

            tool_names = list(self.tool_manager._tools.keys())
            tool_specs = self.tool_manager.cached_tool_specs
            if not enable_memory:
                tool_names = [name for name in tool_names if name != "search_journal_memory"]
                tool_specs = [
                    spec for spec in (tool_specs or [])
                    if spec.get("name") != "search_journal_memory"
                ] or None

            system_msg = {
                "role": "system",
                "content": self.message_handler.build_system_prompt(
                    user_id=user_id,
                    retrieved_memories=retrieved_memories,
                    tool_names=tool_names,
                ),
            }
            model_messages = [system_msg] + self.message_handler.sanitize_input_messages(input_messages)

            for _ in range(max_iter):
                reply_type, response = await self.model_client.call(
                    messages=model_messages,
                    tool_specs=tool_specs,
                    output_type=output_type,
                    stream=stream,
                    store_reply=lambda text: self.message_handler.store_model_reply(
                        text,
                        self._assistant_sender_id,
                    ),
                )

                if reply_type == ReplyType.SIMPLE_REPLY:
                    if not stream:
                        await self.message_handler.store_model_reply(
                            str(response),
                            self._assistant_sender_id,
                        )
                    return response

                if reply_type == ReplyType.STRUCTURED_REPLY:
                    await self.message_handler.store_model_reply(
                        response.model_dump_json(),
                        self._assistant_sender_id,
                    )
                    return response

                if reply_type == ReplyType.TOOL_CALL:
                    tool_result = await self.tool_executor.handle_tool_calls(
                        response,
                        input_messages,
                        max_concurrent_tools,
                    )
                    if tool_result is not None:
                        image_data, description = tool_result
                        await self.message_handler.store_model_reply(
                            description,
                            self._assistant_sender_id,
                        )
                        return image_data
                    model_messages = [system_msg] + self.message_handler.sanitize_input_messages(input_messages)
                    continue

                logger.error("Unknown reply type: %s", reply_type)
                return "Sorry, I encountered an error while processing your request."

            logger.error("Failed to generate response after %d attempts", max_iter)
            return "Sorry, I could not generate a response after multiple attempts."

        except Exception as exc:
            logger.exception("Agent chat error: %s", exc)
            return "Sorry, something went wrong."
