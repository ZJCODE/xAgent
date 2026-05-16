import logging
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Union

from pydantic import BaseModel

from ..components import (
    MarkdownMemory,
    MessageStorageBase,
    MessageStorageLocal,
    MessageStoragePrivateTemp,
)
from ..components.memory import JournalLLMService
from ..integrations.langfuse import NoopObservabilityRuntime, ObservabilityRuntime
from .config import AgentConfig, MemoryMode, ReplyType
from .handlers import MemoryHandler, MessageHandler, ModelClient
from .tools import ToolExecutor, ToolManager
from ..schemas import AgentTurnResult, Message
from ..tools import create_write_memory_tool, create_search_memory_tool


logger = logging.getLogger(__name__)


class Agent:
    """AI agent runtime for a continuous agent-level message stream."""

    _MEMORY_TOOL_NAMES = {"write_memory", "search_memory"}
    _MEMORY_WRITE_TOOL_NAMES = {"write_memory"}

    def __init__(
        self,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        client: Optional[Any] = None,
        model_backend: str = "openai",
        model_max_tokens: int = AgentConfig.DEFAULT_MAX_TOKENS,
        tools: Optional[List] = None,
        output_type: Optional[type[BaseModel]] = None,
        message_storage: Optional[MessageStorageBase] = None,
        workspace: Optional[str] = None,
        observability: Optional[ObservabilityRuntime] = None,
    ):
        self.model = model or AgentConfig.DEFAULT_MODEL
        self.model_backend = model_backend
        self.model_max_tokens = model_max_tokens
        self.observability = observability or NoopObservabilityRuntime()
        self.client = client or self.observability.create_client({})
        if self.client is None:
            if str(self.model_backend).strip().lower() == "anthropic":
                from anthropic import AsyncAnthropic

                self.client = AsyncAnthropic()
            else:
                from openai import AsyncOpenAI

                self.client = AsyncOpenAI()
        self.output_type = output_type
        self.system_prompt = system_prompt or ""
        self._assistant_sender_id = "agent"
        self._memory_mode_var: ContextVar[MemoryMode] = ContextVar(
            f"xagent_memory_mode_{id(self)}",
            default=MemoryMode.FULL,
        )
        self._private_handler: Optional[MessageHandler] = None

        workspace_path: Optional[Path] = None
        if workspace is not None:
            workspace_path = Path(workspace).expanduser().resolve()

        if message_storage is not None:
            self.message_storage = message_storage
        elif workspace_path is not None:
            self.message_storage = MessageStorageLocal(
                path=str(self._message_storage_path(workspace_path))
            )
        else:
            default_workspace = Path(AgentConfig.DEFAULT_WORKSPACE).expanduser().resolve()
            default_workspace.mkdir(parents=True, exist_ok=True)
            self.message_storage = MessageStorageLocal(
                path=str(self._message_storage_path(default_workspace))
            )

        # Markdown-based memory system
        if workspace_path is not None:
            memory_dir = str(self._memory_dir(workspace_path))
        else:
            default_workspace = Path(AgentConfig.DEFAULT_WORKSPACE).expanduser().resolve()
            default_workspace.mkdir(parents=True, exist_ok=True)
            memory_dir = str(self._memory_dir(default_workspace))

        self.markdown_memory = MarkdownMemory(memory_dir=memory_dir)
        self.llm_service = JournalLLMService(
            client=self.client,
            model=self.model,
            backend=self.model_backend,
            max_tokens=self.model_max_tokens,
        )
        self.memory_handler = MemoryHandler(
            memory=self.markdown_memory,
            llm_service=self.llm_service,
        )

        bound_tools = list(tools or [])
        bound_tools.extend([
            create_write_memory_tool(
                memory=self.markdown_memory,
                is_enabled=self._memory_can_write,
            ),
            create_search_memory_tool(
                memory=self.markdown_memory,
                is_enabled=self._memory_can_read,
            ),
        ])
        self.tool_manager = ToolManager(tools=bound_tools)
        self.model_client = ModelClient(
            client=self.client,
            model=self.model,
            backend=self.model_backend,
            max_tokens=self.model_max_tokens,
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

    @property
    def identity(self) -> str:
        return self.system_prompt

    @identity.setter
    def identity(self, value: str) -> None:
        self.set_identity(value)

    def set_identity(self, identity: str) -> None:
        self.system_prompt = identity or ""
        if hasattr(self, "message_handler"):
            self.message_handler.system_prompt = self.system_prompt

    @property
    def tools(self) -> dict:
        return self.tool_manager.tools

    @classmethod
    def _message_storage_path(cls, workspace: Path) -> Path:
        return workspace / AgentConfig.MESSAGE_DIRNAME / AgentConfig.MESSAGE_DB_FILENAME

    @classmethod
    def _memory_dir(cls, workspace: Path) -> Path:
        return workspace / AgentConfig.MEMORY_DIRNAME

    async def flush_memory(self) -> None:
        flusher = getattr(self.memory_handler, "flush_pending", None)
        if flusher is not None:
            await flusher()
        observability_flusher = getattr(self._observability_runtime(), "flush", None)
        if observability_flusher is not None:
            try:
                await observability_flusher()
            except Exception as exc:
                logger.warning("Failed to flush observability events: %s", exc)

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
            private: When True, messages are stored in an isolated temporary
                private buffer (discarded on switch back to normal mode). Memory
                *reads* are preserved but all memory *writes* are suppressed.
        """
        if output_type is None:
            output_type = self.output_type
        if output_type:
            stream = False

        msg_handler = self._message_handler_for_mode(private=private)
        memory_mode = MemoryMode.from_flags(enable_memory=enable_memory, private=private)
        memory_mode_token = self._set_memory_mode(memory_mode)
        model_name = getattr(self, "model", AgentConfig.DEFAULT_MODEL)
        turn_context = self._observability_runtime().agent_turn(
            user_id=user_id,
            model=model_name,
            private=private,
            memory_mode=memory_mode.value,
            stream=stream,
        )
        entered_observability = False

        try:
            turn_context.__enter__()
            entered_observability = True
            user_msg = await msg_handler.store_user_message(
                user_message,
                user_id,
                image_source,
            )

            effective_history_count = self._effective_history_count(history_count)
            recent_messages = await msg_handler.get_recent_messages(
                history_count=effective_history_count,
            )

            memory_context = ""
            if memory_mode.can_read:
                memory_context = await self.memory_handler.get_recent_context()

            excluded = self._excluded_memory_tools(memory_mode=memory_mode)
            tool_names = [n for n in self.tool_manager._tools if n not in excluded]
            tool_specs = self.tool_manager.cached_tool_specs
            if excluded and tool_specs:
                tool_specs = [s for s in tool_specs if self._tool_spec_name(s) not in excluded] or None

            instructions = msg_handler.build_instruction_messages(tool_names=tool_names)
            iteration_messages = msg_handler.build_turn_context_messages(
                recent_messages,
                current_user_id=user_id,
                memory_context=memory_context,
            )
            input_messages = msg_handler.sanitize_input_messages(list(iteration_messages))

            for _ in range(max_iter):
                reply_type, response = await self.model_client.call(
                    messages=input_messages,
                    tool_specs=tool_specs,
                    instructions=instructions,
                    output_type=output_type,
                    stream=stream,
                    store_reply=lambda text: self._store_reply_and_schedule_experience(
                        msg_handler=msg_handler,
                        memory_mode=memory_mode,
                        triggering_messages=[user_msg],
                        reply_text=text,
                    ),
                )

                if reply_type == ReplyType.SIMPLE_REPLY:
                    if not stream:
                        assistant_msg = await msg_handler.store_model_reply(
                            str(response),
                            self._assistant_sender_id,
                        )
                        self._schedule_experience_write(
                            msg_handler=msg_handler,
                            memory_mode=memory_mode,
                            messages=[user_msg, assistant_msg],
                        )
                    return response

                if reply_type == ReplyType.STRUCTURED_REPLY:
                    assistant_msg = await msg_handler.store_model_reply(
                        response.model_dump_json(),
                        self._assistant_sender_id,
                    )
                    self._schedule_experience_write(
                        msg_handler=msg_handler,
                        memory_mode=memory_mode,
                        messages=[user_msg, assistant_msg],
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
                        assistant_msg = await msg_handler.store_model_reply(
                            description,
                            self._assistant_sender_id,
                        )
                        self._schedule_experience_write(
                            msg_handler=msg_handler,
                            memory_mode=memory_mode,
                            messages=[user_msg, assistant_msg],
                        )
                        return image_data
                    input_messages = msg_handler.sanitize_input_messages(list(iteration_messages))
                    continue

                if reply_type == ReplyType.ERROR:
                    logger.error("Model returned error event: %s", response)
                    return "Sorry, I encountered an error while processing your request."

                logger.error("Unknown reply type: %s", reply_type)
                return "Sorry, I encountered an error while processing your request."

            logger.error("Failed to generate response after %d attempts", max_iter)
            return "Sorry, I could not generate a response after multiple attempts."

        except Exception as exc:
            logger.exception("Agent chat error: %s", exc)
            return "Sorry, something went wrong."
        finally:
            if entered_observability:
                try:
                    turn_context.__exit__(None, None, None)
                except Exception as exc:
                    logger.warning("Failed to close observability context: %s", exc)
            self._reset_memory_mode(memory_mode_token)

    async def observe(
        self,
        context: str,
        source: str = "environment",
        event_type: str = "observation",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AgentTurnResult:
        """Record environmental context without generating a reply."""
        event_msg = await self.message_handler.store_context_event(
            context=context,
            source=source,
            event_type=event_type,
            metadata=metadata,
        )
        self._schedule_experience_write(
            msg_handler=self.message_handler,
            memory_mode=MemoryMode.FULL,
            messages=[event_msg],
        )
        event_metadata = event_msg.metadata or {}
        return AgentTurnResult(
            kind="observe",
            replied=False,
            reply=None,
            event_id=event_msg.timestamp,
            event_type=event_metadata.get("event_type"),
            source=event_metadata.get("source"),
        )

    def _message_handler_for_mode(self, private: bool) -> MessageHandler:
        """Return the storage handler for normal or private mode."""
        if private and self._private_handler is None:
            self._private_handler = MessageHandler(
                message_storage=MessageStoragePrivateTemp(),
                system_prompt=self.system_prompt,
            )
        elif not private and self._private_handler is not None:
            self._private_handler = None
        return self._private_handler or self.message_handler

    def _observability_runtime(self) -> ObservabilityRuntime:
        observability = getattr(self, "observability", None)
        if observability is None:
            observability = NoopObservabilityRuntime()
            self.observability = observability
        return observability

    async def _store_reply_and_schedule_experience(
        self,
        msg_handler: MessageHandler,
        memory_mode: MemoryMode,
        triggering_messages: List[Message],
        reply_text: str,
    ) -> None:
        assistant_msg = await msg_handler.store_model_reply(
            reply_text,
            self._assistant_sender_id,
        )
        self._schedule_experience_write(
            msg_handler=msg_handler,
            memory_mode=memory_mode,
            messages=[*triggering_messages, assistant_msg],
        )

    def _schedule_experience_write(
        self,
        msg_handler: MessageHandler,
        memory_mode: MemoryMode,
        messages: List[Message],
        caused_reply: bool = False,
    ) -> None:
        if not memory_mode.can_write or not messages:
            return

        scheduler = getattr(self.memory_handler, "schedule_experience_write", None)
        if scheduler is not None:
            scheduler(messages, caused_reply=caused_reply)
            return

        conversation_messages = msg_handler.filter_conversation_messages(messages)
        if conversation_messages:
            self.memory_handler.schedule_diary_write(
                msg_handler.to_model_input(conversation_messages)
            )

    def _excluded_memory_tools(
        self,
        enable_memory: bool = True,
        private: bool = False,
        memory_mode: Optional[MemoryMode] = None,
    ) -> set:
        """Return the set of memory tool names to exclude from this call."""
        mode = memory_mode or MemoryMode.from_flags(enable_memory=enable_memory, private=private)
        if mode == MemoryMode.DISABLED:
            return self._MEMORY_TOOL_NAMES
        if mode == MemoryMode.READ_ONLY:
            return self._MEMORY_WRITE_TOOL_NAMES
        return set()

    def _get_memory_mode_var(self) -> ContextVar[MemoryMode]:
        memory_mode_var = getattr(self, "_memory_mode_var", None)
        if memory_mode_var is None:
            memory_mode_var = ContextVar(
                f"xagent_memory_mode_{id(self)}",
                default=MemoryMode.FULL,
            )
            self._memory_mode_var = memory_mode_var
        return memory_mode_var

    def _set_memory_mode(self, memory_mode: MemoryMode) -> Token:
        return self._get_memory_mode_var().set(memory_mode)

    def _reset_memory_mode(self, token: Token) -> None:
        self._get_memory_mode_var().reset(token)

    def _current_memory_mode(self) -> MemoryMode:
        return self._get_memory_mode_var().get()

    def _memory_can_read(self) -> bool:
        return self._current_memory_mode().can_read

    def _memory_can_write(self) -> bool:
        return self._current_memory_mode().can_write

    @staticmethod
    def _effective_history_count(history_count: Optional[int]) -> int:
        requested = history_count or AgentConfig.DEFAULT_HISTORY_COUNT
        try:
            requested_count = int(requested)
        except (TypeError, ValueError):
            requested_count = AgentConfig.DEFAULT_HISTORY_COUNT
        capped = min(requested_count, AgentConfig.MAX_TRANSCRIPT_MESSAGES)
        return max(1, capped)

    @staticmethod
    def _tool_spec_name(tool_spec: dict) -> str:
        return tool_spec.get("function", {}).get("name") or tool_spec.get("name")
