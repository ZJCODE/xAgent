import logging
from pathlib import Path
from typing import AsyncGenerator, List, Optional, Union

from openai import AsyncOpenAI
from pydantic import BaseModel

from ..components import MessageStorageBase, MessageStorageLocal, MemoryStorageBase, MemoryStorageLocal
from .config import AgentConfig, ReplyType
from .handlers import MemoryManager, MessageHandler, ModelClient
from .session import normalize_conversation_id
from .tools import ToolExecutor, ToolManager


logger = logging.getLogger(__name__)


class Agent:
    """AI agent runtime for a unified conversation transcript model."""

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

        workspace_path: Optional[Path] = None
        if workspace is not None:
            workspace_path = Path(workspace).expanduser().resolve()

        if message_storage is not None:
            self.message_storage = message_storage
        elif workspace_path is not None:
            self.message_storage = MessageStorageLocal(path=str(workspace_path / "messages.sqlite3"))
        else:
            self.message_storage = MessageStorageLocal()

        if memory_storage is not None:
            self.memory_storage = memory_storage
        elif workspace_path is not None:
            self.memory_storage = MemoryStorageLocal(
                path=str(workspace_path / "chroma"),
                collection_name=self.name,
            )
        else:
            self.memory_storage = MemoryStorageLocal(collection_name=self.name)

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
        self._assistant_sender_id = f"agent:{self.name}"
        self._agent_memory_key = f"agent:{self.name}"

    @property
    def tools(self) -> dict:
        return self.tool_manager.tools

    @property
    def mcp_tools(self) -> dict:
        return self.tool_manager.mcp_tools

    @property
    def memory_key(self) -> str:
        return self._agent_memory_key

    def normalize_conversation_id(self, conversation_id: str) -> str:
        return normalize_conversation_id(self.name, conversation_id)

    async def __call__(
        self,
        user_message: str,
        user_id: str = AgentConfig.DEFAULT_USER_ID,
        conversation_id: str = AgentConfig.DEFAULT_CONVERSATION_ID,
        history_count: int = AgentConfig.DEFAULT_HISTORY_COUNT,
        max_iter: int = AgentConfig.DEFAULT_MAX_ITER,
        max_concurrent_tools: int = AgentConfig.DEFAULT_MAX_CONCURRENT_TOOLS,
        image_source: Optional[Union[str, List[str]]] = None,
        output_type: Optional[type[BaseModel]] = None,
        stream: bool = False,
        enable_memory: bool = False,
    ) -> Union[str, BaseModel, AsyncGenerator[str, None]]:
        return await self.chat(
            user_message=user_message,
            user_id=user_id,
            conversation_id=conversation_id,
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
        conversation_id: str = AgentConfig.DEFAULT_CONVERSATION_ID,
        history_count: int = AgentConfig.DEFAULT_HISTORY_COUNT,
        max_iter: int = AgentConfig.DEFAULT_MAX_ITER,
        max_concurrent_tools: int = AgentConfig.DEFAULT_MAX_CONCURRENT_TOOLS,
        image_source: Optional[Union[str, List[str]]] = None,
        output_type: Optional[type[BaseModel]] = None,
        stream: bool = False,
        enable_memory: bool = False,
    ) -> Union[str, BaseModel, AsyncGenerator[str, None]]:
        """Generate a reply from the agent given a user message."""
        normalized_conversation_id = self.normalize_conversation_id(conversation_id)

        if output_type is None:
            output_type = self.output_type
        if output_type:
            stream = False

        try:
            await self.tool_manager.ensure_mcp_ready()

            await self.message_handler.store_user_message(
                user_message,
                user_id,
                normalized_conversation_id,
                image_source,
            )

            input_messages = await self.message_handler.get_input_messages(
                conversation_id=normalized_conversation_id,
                history_count=history_count,
            )
            messages_without_tool = self.message_handler.filter_non_tool_messages(input_messages)

            retrieved_memories: list = []
            if enable_memory:
                retrieved_memories = await self.memory_manager.retrieve_memories(
                    memory_key=self.memory_key,
                    query=user_message,
                )
                self.memory_manager.schedule_memory_add(
                    memory_key=self.memory_key,
                    conversation_id=normalized_conversation_id,
                    messages=messages_without_tool[-2:],
                )

            system_msg = {
                "role": "system",
                "content": self.message_handler.build_system_prompt(
                    user_id=user_id,
                    retrieved_memories=retrieved_memories,
                    tool_names=list(self.tool_manager._tools.keys()),
                ),
            }
            model_messages = [system_msg] + self.message_handler.sanitize_input_messages(input_messages)

            for _ in range(max_iter):
                reply_type, response = await self.model_client.call(
                    messages=model_messages,
                    tool_specs=self.tool_manager.cached_tool_specs,
                    output_type=output_type,
                    stream=stream,
                    store_reply=lambda text: self.message_handler.store_model_reply(
                        text,
                        normalized_conversation_id,
                        self._assistant_sender_id,
                    ),
                )

                if reply_type == ReplyType.SIMPLE_REPLY:
                    if not stream:
                        await self.message_handler.store_model_reply(
                            str(response),
                            normalized_conversation_id,
                            self._assistant_sender_id,
                        )
                    return response

                if reply_type == ReplyType.STRUCTURED_REPLY:
                    await self.message_handler.store_model_reply(
                        response.model_dump_json(),
                        normalized_conversation_id,
                        self._assistant_sender_id,
                    )
                    return response

                if reply_type == ReplyType.TOOL_CALL:
                    tool_result = await self.tool_executor.handle_tool_calls(
                        response,
                        normalized_conversation_id,
                        input_messages,
                        max_concurrent_tools,
                    )
                    if tool_result is not None:
                        image_data, description = tool_result
                        await self.message_handler.store_model_reply(
                            description,
                            normalized_conversation_id,
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
