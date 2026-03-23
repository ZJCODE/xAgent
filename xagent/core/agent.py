import logging
from pathlib import Path
from typing import AsyncGenerator, List, Optional, Union

from openai import AsyncOpenAI
from pydantic import BaseModel

from ..components import MessageStorageBase, MessageStorageInMemory, MessageStorageLocal
from ..components.memory.markdown_memory import MarkdownMemory
from ..components.memory.helper.llm_service import JournalLLMService
from .config import AgentConfig, ReplyType
from .handlers import MemoryHandler, MessageHandler, ModelClient
from .tools import ToolExecutor, ToolManager
from ..tools import create_write_daily_memory_tool, create_search_memory_tool, create_generate_summary_tool


logger = logging.getLogger(__name__)


def _normalize_agent_identifier(name: str) -> str:
    return (name or AgentConfig.DEFAULT_NAME).lower().replace(" ", "_").replace("-", "_")


class Agent:
    """AI agent runtime for a continuous agent-level message stream."""

    _MEMORY_TOOL_NAMES = {"write_daily_memory", "search_memory", "generate_memory_summary"}
    _MEMORY_WRITE_TOOL_NAMES = {"write_daily_memory", "generate_memory_summary"}
    _MEMORY_READ_TOOL_NAMES = {"search_memory"}

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
        workspace: Optional[str] = None,
    ):
        self.name = name or AgentConfig.DEFAULT_NAME
        self.description = description
        self.model = model or AgentConfig.DEFAULT_MODEL
        self.client = client or AsyncOpenAI()
        self.output_type = output_type
        self.system_prompt = system_prompt or ""
        self._assistant_sender_id = f"agent:{self.name}"
        self._memory_tools_enabled = True
        self._private_mode = False
        self._private_storage: Optional[MessageStorageInMemory] = None
        self._private_message_handler: Optional[MessageHandler] = None

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

        # Markdown-based memory system
        if workspace_path is not None:
            memory_dir = str(workspace_path / f"{_normalize_agent_identifier(self.name)}_memory")
        else:
            default_workspace = Path(AgentConfig.DEFAULT_WORKSPACE).expanduser().resolve()
            default_workspace.mkdir(parents=True, exist_ok=True)
            memory_dir = str(default_workspace / f"{_normalize_agent_identifier(self.name)}_memory")

        self.markdown_memory = MarkdownMemory(memory_dir=memory_dir)
        self.llm_service = JournalLLMService(model=self.model)
        self.memory_handler = MemoryHandler(
            memory=self.markdown_memory,
            llm_service=self.llm_service,
        )

        bound_tools = list(tools or [])
        bound_tools.extend([
            create_write_daily_memory_tool(
                memory=self.markdown_memory,
                is_enabled=lambda: self._memory_tools_enabled,
            ),
            create_search_memory_tool(
                memory=self.markdown_memory,
                is_enabled=lambda: self._memory_tools_enabled,
            ),
            create_generate_summary_tool(
                memory=self.markdown_memory,
                llm_service=self.llm_service,
                is_enabled=lambda: self._memory_tools_enabled,
            ),
        ])
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
        private: bool = False,
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
            private=private,
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
        private: bool = False,
    ) -> Union[str, BaseModel, AsyncGenerator[str, None]]:
        """Generate a reply from the agent given a user message.

        Args:
            private: When True, messages are stored in an isolated in-memory
                buffer (discarded on switch back to normal mode). Memory
                *reads* are preserved but all memory *writes* are suppressed.
        """
        if output_type is None:
            output_type = self.output_type
        if output_type:
            stream = False

        # --- Private-mode lifecycle management ---
        msg_handler = self._resolve_message_handler(private)

        try:
            await self.tool_manager.ensure_mcp_ready()

            # Determine memory write / read flags
            memory_read = enable_memory  # private keeps reads if enable_memory is True
            memory_write = enable_memory and not private
            self._memory_tools_enabled = memory_write

            await msg_handler.store_user_message(
                user_message,
                user_id,
                image_source,
            )

            recent_messages = await msg_handler.get_recent_messages(
                history_count=history_count,
            )
            conversation_messages = msg_handler.filter_conversation_messages(recent_messages)
            messages_without_tool = msg_handler.to_model_input(conversation_messages)

            memory_context = ""
            if memory_read:
                memory_context = await self.memory_handler.get_recent_context()
            if memory_write:
                self.memory_handler.schedule_diary_write(messages_without_tool[-2:])

            # Filter tool specs based on memory flags
            tool_names = list(self.tool_manager._tools.keys())
            tool_specs = self.tool_manager.cached_tool_specs
            if not enable_memory:
                # memory completely off: remove all memory tools
                tool_names = [n for n in tool_names if n not in self._MEMORY_TOOL_NAMES]
                tool_specs = [
                    spec for spec in (tool_specs or [])
                    if spec.get("name") not in self._MEMORY_TOOL_NAMES
                ] or None
            elif private:
                # private mode: remove write tools, keep read tools
                tool_names = [n for n in tool_names if n not in self._MEMORY_WRITE_TOOL_NAMES]
                tool_specs = [
                    spec for spec in (tool_specs or [])
                    if spec.get("name") not in self._MEMORY_WRITE_TOOL_NAMES
                ] or None

            instructions = msg_handler.build_instructions(tool_names=tool_names)
            iteration_messages = [
                msg_handler.build_recent_transcript_message(
                    recent_messages,
                    current_user_id=user_id,
                    memory_context=memory_context,
                )
            ]
            input_messages = msg_handler.sanitize_input_messages(list(iteration_messages))

            for _ in range(max_iter):
                reply_type, response = await self.model_client.call(
                    messages=input_messages,
                    tool_specs=tool_specs,
                    instructions=instructions,
                    output_type=output_type,
                    stream=stream,
                    store_reply=lambda text: msg_handler.store_model_reply(
                        text,
                        self._assistant_sender_id,
                    ),
                )

                if reply_type == ReplyType.SIMPLE_REPLY:
                    if not stream:
                        await msg_handler.store_model_reply(
                            str(response),
                            self._assistant_sender_id,
                        )
                    return response

                if reply_type == ReplyType.STRUCTURED_REPLY:
                    await msg_handler.store_model_reply(
                        response.model_dump_json(),
                        self._assistant_sender_id,
                    )
                    return response

                if reply_type == ReplyType.TOOL_CALL:
                    tool_result = await self.tool_executor.handle_tool_calls(
                        response,
                        iteration_messages,
                        max_concurrent_tools,
                    )
                    if tool_result is not None:
                        image_data, description = tool_result
                        await msg_handler.store_model_reply(
                            description,
                            self._assistant_sender_id,
                        )
                        return image_data
                    input_messages = msg_handler.sanitize_input_messages(list(iteration_messages))
                    continue

                logger.error("Unknown reply type: %s", reply_type)
                return "Sorry, I encountered an error while processing your request."

            logger.error("Failed to generate response after %d attempts", max_iter)
            return "Sorry, I could not generate a response after multiple attempts."

        except Exception as exc:
            logger.exception("Agent chat error: %s", exc)
            return "Sorry, something went wrong."

    def _resolve_message_handler(self, private: bool) -> MessageHandler:
        """Return the appropriate MessageHandler for the current mode.

        When entering private mode, lazily creates an in-memory storage and
        handler.  When leaving private mode, discards them.
        """
        if private:
            if not self._private_mode:
                # Entering private mode — create isolated storage
                self._private_storage = MessageStorageInMemory()
                self._private_message_handler = MessageHandler(
                    message_storage=self._private_storage,
                    system_prompt=self.system_prompt,
                )
            self._private_mode = True
            return self._private_message_handler  # type: ignore[return-value]

        if self._private_mode:
            # Leaving private mode — discard private state
            self._private_storage = None
            self._private_message_handler = None
            self._private_mode = False

        return self.message_handler
