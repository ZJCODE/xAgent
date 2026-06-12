import logging
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Union

from ..components import (
    MarkdownMemory,
    MessageStorageBase,
    MessageStorageLocal,
    SkillsStorageBase,
)
from ..components.memory import JournalLLMService
from ..integrations.langfuse import NoopObservabilityRuntime, ObservabilityRuntime
from .config import AgentConfig, ReplyType
from .handlers import MemoryHandler, MessageHandler, ModelClient
from .providers import MODEL_API_OPENAI_RESPONSES, model_api_uses_anthropic_client, normalize_model_api
from .tools import ToolExecutor, ToolManager
from ..schemas import AgentTurnResult, Message
from ..tools import create_write_memory_tool, create_search_memory_tool
logger = logging.getLogger(__name__)


class Agent:
    """AI agent runtime for a continuous agent-level message stream."""

    def __init__(
        self,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        client: Optional[Any] = None,
        model_api: str = MODEL_API_OPENAI_RESPONSES,
        model_max_tokens: int = AgentConfig.DEFAULT_MAX_TOKENS,
        tools: Optional[List] = None,
        message_storage: Optional[MessageStorageBase] = None,
        workspace: Optional[str] = None,
        skills_storage: Optional[SkillsStorageBase] = None,
        observability: Optional[ObservabilityRuntime] = None,
        supports_vision: bool = True,
        max_history: int = AgentConfig.DEFAULT_MAX_HISTORY,
        max_iter: int = AgentConfig.DEFAULT_MAX_ITER,
        max_concurrent_tools: int = AgentConfig.DEFAULT_MAX_CONCURRENT_TOOLS,
    ):
        self.model = model or AgentConfig.DEFAULT_MODEL
        self.model_api = normalize_model_api(model_api)
        self.model_max_tokens = model_max_tokens
        self.supports_vision = bool(supports_vision)
        self.max_history = max_history
        self.max_iter = max_iter
        self.max_concurrent_tools = max_concurrent_tools
        self.observability = observability or NoopObservabilityRuntime()
        self.client = client
        if self.client is None:
            if model_api_uses_anthropic_client(self.model_api):
                from anthropic import AsyncAnthropic

                self.client = AsyncAnthropic()
            else:
                from openai import AsyncOpenAI

                self.client = self.observability.create_client({}) or AsyncOpenAI()
        self.system_prompt = system_prompt or ""
        self._assistant_sender_id = "agent"

        workspace_path: Optional[Path] = None
        if workspace is not None:
            workspace_path = Path(workspace).expanduser().resolve()

        runtime_root = workspace_path or Path(AgentConfig.DEFAULT_WORKSPACE).expanduser().resolve()
        runtime_root.mkdir(parents=True, exist_ok=True)
        self.workspace = runtime_root
        self.workspace_dir = self._workspace_dir(runtime_root)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.skills_storage = skills_storage

        if message_storage is not None:
            self.message_storage = message_storage
        elif workspace_path is not None:
            self.message_storage = MessageStorageLocal(
                path=str(self._message_storage_path(workspace_path))
            )
        else:
            self.message_storage = MessageStorageLocal(
                path=str(self._message_storage_path(runtime_root))
            )

        # Markdown-based memory system
        if workspace_path is not None:
            memory_dir = str(self._memory_dir(workspace_path))
        else:
            memory_dir = str(self._memory_dir(runtime_root))

        self.markdown_memory = MarkdownMemory(memory_dir=memory_dir)
        self.llm_service = JournalLLMService(
            client=self.client,
            model=self.model,
            model_api=self.model_api,
            max_tokens=self.model_max_tokens,
        )
        self.memory_handler = MemoryHandler(
            memory=self.markdown_memory,
            llm_service=self.llm_service,
            message_storage=self.message_storage,
            max_history=self.max_history,
        )

        bound_tools = list(tools or [])
        bound_tools.extend([
            create_write_memory_tool(
                memory=self.markdown_memory,
                is_enabled=True,
            ),
            create_search_memory_tool(
                memory=self.markdown_memory,
                is_enabled=True,
                message_storage=self.message_storage,
            ),
        ])
        self.tool_manager = ToolManager(tools=bound_tools)
        self.model_client = ModelClient(
            client=self.client,
            model=self.model,
            model_api=self.model_api,
            max_tokens=self.model_max_tokens,
        )
        self.message_handler = MessageHandler(
            message_storage=self.message_storage,
            system_prompt=self.system_prompt,
            workspace_dir=getattr(self, "workspace_dir", None),
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

    @classmethod
    def _workspace_dir(cls, workspace: Path) -> Path:
        return workspace / AgentConfig.WORKSPACE_DIRNAME

    def _skills_catalog_context(self) -> str:
        skills_storage = getattr(self, "skills_storage", None)
        if skills_storage is None:
            return ""
        return skills_storage.catalog_text(max_chars=AgentConfig.MAX_SKILLS_CATALOG_CHARS)

    def _workspace_context(self, tool_names: List[str]) -> str:
        if "run_command" not in tool_names:
            return ""
        return AgentConfig.build_workspace_context(str(self.workspace_dir))

    async def _build_turn_context(
        self,
        msg_handler: MessageHandler,
        user_msg: Message,
        user_id: str,
    ):
        """Build the shared turn preparation context for both chat and chat_events."""
        recent_messages = await msg_handler.get_recent_messages(
            max_history=self.max_history,
        )
        memory_context = await self.memory_handler.get_recent_context()
        tool_names = list(self.tool_manager._tools)
        tool_specs = self.tool_manager.cached_tool_specs
        workspace_context = self._workspace_context(tool_names)
        skills_catalog = self._skills_catalog_context()
        instructions = msg_handler.build_instruction_messages(
            tool_names=tool_names,
            skills_catalog=skills_catalog,
            supports_vision=self.supports_vision,
            workspace_context=workspace_context,
        )
        iteration_messages = msg_handler.build_turn_context_messages(
            recent_messages,
            current_user_id=user_id,
            memory_context=memory_context,
            max_messages=self.max_history,
            include_images=self.supports_vision,
            workspace_dir=getattr(self, "workspace_dir", None),
            current_message=user_msg,
        )
        input_messages = msg_handler.sanitize_input_messages(list(iteration_messages))
        return tool_specs, instructions, iteration_messages, input_messages

    async def run_memory_maintenance(self) -> None:
        idle_timeout = AgentConfig.IDLE_DIARY_TIMEOUT_SECONDS
        force = False
        if idle_timeout > 0:
            memory = getattr(self, "markdown_memory", None)
            if memory is not None:
                path = memory.root / ".last_interaction"
                try:
                    last = float(path.read_text(encoding="utf-8").strip())
                except (FileNotFoundError, ValueError):
                    last = time.time()
                if time.time() - last >= idle_timeout:
                    force = True

        await self.memory_handler.run_maintenance(force=force)
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
        image_source: Optional[Union[str, List[str]]] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        stream: bool = False,
    ) -> Union[str, AsyncGenerator[str, None]]:
        return await self.chat(
            user_message=user_message,
            user_id=user_id,
            image_source=image_source,
            attachments=attachments,
            stream=stream,
        )

    async def chat(
        self,
        user_message: str,
        user_id: str = AgentConfig.DEFAULT_USER_ID,
        image_source: Optional[Union[str, List[str]]] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        stream: bool = False,
    ) -> Union[str, AsyncGenerator[str, None]]:
        """Generate a reply from the agent given a user message.

        Args:
            stream: When True, return an async text generator for compatibility
                with the legacy Python API. New event consumers should prefer
                ``chat_events(stream=True)``.
        """
        self._record_last_interaction()
        if stream:
            async def text_stream():
                streamed_message_ids: set[str] = set()
                async for event in self.chat_events(
                    user_message=user_message,
                    user_id=user_id,
                    image_source=image_source,
                    attachments=attachments,
                    stream=True,
                ):
                    event_type = event.get("type")
                    message_id = str(event.get("message_id") or "")
                    if event_type == "message_delta":
                        if message_id:
                            streamed_message_ids.add(message_id)
                        yield str(event.get("delta") or "")
                    elif event_type == "message_done" and message_id not in streamed_message_ids:
                        yield str(event.get("content") or "")
                    elif event_type == "error":
                        yield str(event.get("error") or "")

            return text_stream()

        if hasattr(self.model_client, "model_turn_events"):
            final_reply = ""
            last_error = ""
            async for event in self.chat_events(
                user_message=user_message,
                user_id=user_id,
                image_source=image_source,
                attachments=attachments,
                stream=False,
            ):
                if event.get("type") == "message_done" and event.get("phase") == "final":
                    final_reply = str(event.get("content") or "")
                elif event.get("type") == "error":
                    last_error = str(event.get("error") or "")
            return final_reply or last_error

        msg_handler = self.message_handler
        model_name = getattr(self, "model", AgentConfig.DEFAULT_MODEL)
        turn_ctx = self._observability_runtime().agent_turn(
            user_id=user_id,
            model=model_name,
            memory_mode="full",
            stream=False,
        )
        try:
            with turn_ctx as turn_obs:
                try:
                    user_msg = await msg_handler.store_user_message(
                        user_message,
                        user_id,
                        image_source,
                        attachments=attachments,
                    )
                except ValueError as exc:
                    logger.warning("Invalid image input from %s: %s", user_id, exc)
                    return str(exc)

                tool_specs, instructions, iteration_messages, input_messages = await self._build_turn_context(
                    msg_handler=msg_handler,
                    user_msg=user_msg,
                    user_id=user_id,
                )
                turn_obs.set_input(input_messages)

                for _ in range(self.max_iter):
                    logger.debug("Agent iteration with input messages: %s", input_messages)
                    reply_type, response = await self.model_client.call(
                        messages=input_messages,
                        tool_specs=tool_specs,
                        instructions=instructions,
                        stream=False,
                        store_reply=lambda text: self._store_reply_and_schedule_experience(
                            msg_handler=msg_handler,
                            triggering_messages=[user_msg],
                            reply_text=text,
                        ),
                    )

                    if reply_type == ReplyType.SIMPLE_REPLY:
                        assistant_msg = await msg_handler.store_model_reply(
                            str(response),
                            self._assistant_sender_id,
                        )
                        self._schedule_experience_write(
                            messages=[user_msg, assistant_msg],
                        )
                        turn_obs.set_output(str(response))
                        return response

                    if reply_type == ReplyType.TOOL_CALL:
                        tool_result = await self.tool_executor.handle_tool_calls(
                            response,
                            iteration_messages,
                            self.max_concurrent_tools,
                        )
                        if tool_result is not None:
                            assistant_msg = await msg_handler.store_model_reply(
                                tool_result.description,
                                self._assistant_sender_id,
                                attachments=tool_result.attachments,
                            )
                            self._schedule_experience_write(
                                messages=[user_msg, assistant_msg],
                            )
                            turn_obs.set_output(tool_result.content)
                            return tool_result.content
                        input_messages = msg_handler.sanitize_input_messages(list(iteration_messages))
                        continue

                    if reply_type == ReplyType.ERROR:
                        logger.error("Model returned error event: %s", response)
                        return "Sorry, I encountered an error while processing your request."

                    logger.error("Unknown reply type: %s", reply_type)
                    return "Sorry, I encountered an error while processing your request."

                logger.error("Failed to generate response after %d attempts", self.max_iter)
                return "Sorry, I could not generate a response after multiple attempts."
        except Exception as exc:
            logger.exception("Agent chat error: %s", exc)
            return "Sorry, something went wrong."
        finally:
            try:
                await self._observability_runtime().flush()
            except Exception as exc:
                logger.warning("Failed to flush observability events: %s", exc)

    async def chat_events(
        self,
        user_message: str,
        user_id: str = AgentConfig.DEFAULT_USER_ID,
        image_source: Optional[Union[str, List[str]]] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        stream: bool = False,
    ) -> AsyncGenerator[dict, None]:
        """Emit one agent turn as structured message/tool events.

        ``stream`` only controls whether text is additionally exposed as
        ``message_delta`` events. Message boundaries and tool progress are
        always eventized.
        """
        self._record_last_interaction()
        msg_handler = self.message_handler
        model_name = getattr(self, "model", AgentConfig.DEFAULT_MODEL)
        turn_ctx = self._observability_runtime().agent_turn(
            user_id=user_id,
            model=model_name,
            memory_mode="full",
            stream=stream,
        )
        with turn_ctx as turn_obs:
            try:
                user_msg = await msg_handler.store_user_message(
                    user_message,
                    user_id,
                    image_source,
                    attachments=attachments,
                )
            except ValueError as exc:
                logger.warning("Invalid image input from %s: %s", user_id, exc)
                yield {"type": "error", "error": str(exc), "status_code": 400}
                yield {"type": "done"}
                return

            tool_specs, instructions, iteration_messages, input_messages = await self._build_turn_context(
                msg_handler=msg_handler,
                user_msg=user_msg,
                user_id=user_id,
            )
            turn_obs.set_input(input_messages)

            for iteration_index in range(self.max_iter):
                message_id = self._turn_message_id(user_msg, iteration_index)
                text_parts: list[str] = []
                tool_calls = []
                message_started = False

                def ensure_live_message_started() -> dict:
                    nonlocal message_started
                    message_started = True
                    return self._message_start_event(message_id, "assistant")

                async for model_event in self.model_client.model_turn_events(
                    messages=input_messages,
                    tool_specs=tool_specs,
                    instructions=instructions,
                    stream=stream,
                ):
                    if model_event.type in {"delta", "text"} and model_event.delta:
                        text_parts.append(model_event.delta)
                        if stream:
                            if not message_started:
                                yield ensure_live_message_started()
                            yield self._message_delta_event(
                                message_id,
                                "assistant",
                                model_event.delta,
                            )
                        continue

                    if model_event.type == "tool_calls":
                        tool_calls = model_event.tool_calls
                        continue

                    if model_event.type == "error":
                        logger.error("Model stream returned error event: %s", model_event.error)
                        yield {
                            "type": "error",
                            "error": "Sorry, I encountered an error while processing your request.",
                        }
                        yield {"type": "done"}
                        return

                visible_text = "".join(text_parts)
                if tool_calls:
                    if visible_text:
                        if message_started:
                            yield self._message_done_event(message_id, "preface", visible_text)
                        else:
                            for event in self._message_events(
                                message_id=message_id,
                                phase="preface",
                                content=visible_text,
                                stream=stream,
                                deltas=text_parts,
                            ):
                                yield event
                        await msg_handler.store_model_reply(
                            visible_text,
                            self._assistant_sender_id,
                            metadata={"turn_phase": "preface"},
                        )

                    for tool_call in tool_calls:
                        yield self._tool_event("tool_call", tool_call)

                    tool_result = await self.tool_executor.handle_tool_calls(
                        tool_calls,
                        iteration_messages,
                        self.max_concurrent_tools,
                    )

                    for tool_call in tool_calls:
                        yield self._tool_event("tool_result", tool_call)

                    if tool_result is not None:
                        final_message_id = self._turn_message_id(user_msg, iteration_index, suffix="image")
                        for event in self._message_events(
                            message_id=final_message_id,
                            phase="final",
                            content=tool_result.content,
                            stream=False,
                            attachments=tool_result.attachments,
                        ):
                            yield event
                        assistant_msg = await msg_handler.store_model_reply(
                            tool_result.description,
                            self._assistant_sender_id,
                            metadata={"turn_phase": "final"},
                            attachments=tool_result.attachments,
                        )
                        self._schedule_experience_write(
                            messages=[user_msg, assistant_msg],
                        )
                        turn_obs.set_output(tool_result.content)
                        yield {"type": "done"}
                        return

                    input_messages = msg_handler.sanitize_input_messages(list(iteration_messages))
                    continue

                if visible_text:
                    if message_started:
                        yield self._message_done_event(message_id, "final", visible_text)
                    else:
                        for event in self._message_events(
                            message_id=message_id,
                            phase="final",
                            content=visible_text,
                            stream=stream,
                            deltas=text_parts,
                        ):
                            yield event
                    assistant_msg = await msg_handler.store_model_reply(
                        visible_text,
                        self._assistant_sender_id,
                        metadata={"turn_phase": "final"},
                    )
                    self._schedule_experience_write(
                        messages=[user_msg, assistant_msg],
                    )
                    turn_obs.set_output(visible_text)
                    yield {"type": "done"}
                    return

                logger.error("Model stream ended without text or tool calls")
                yield {
                    "type": "error",
                    "error": "Sorry, I encountered an error while processing your request.",
                }
                yield {"type": "done"}
                return

            logger.error("Failed to generate response after %d attempts", self.max_iter)
            yield {
                "type": "error",
                "error": "Sorry, I could not generate a response after multiple attempts.",
            }
            yield {"type": "done"}

        # Flush after turn context exits
        try:
            await self._observability_runtime().flush()
        except Exception as exc:
            logger.warning("Failed to flush observability events: %s", exc)

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

    def _observability_runtime(self) -> ObservabilityRuntime:
        observability = getattr(self, "observability", None)
        if observability is None:
            observability = NoopObservabilityRuntime()
            self.observability = observability
        return observability

    def _record_last_interaction(self) -> None:
        """Write the current timestamp to the shared idle-tracking file."""
        memory = getattr(self, "markdown_memory", None)
        if memory is None:
            return
        path = memory.root / ".last_interaction"
        try:
            path.write_text(str(time.time()), encoding="utf-8")
        except OSError:
            pass

    async def _store_reply_and_schedule_experience(
        self,
        msg_handler: MessageHandler,
        triggering_messages: List[Message],
        reply_text: str,
    ) -> None:
        assistant_msg = await msg_handler.store_model_reply(
            reply_text,
            self._assistant_sender_id,
        )
        self._schedule_experience_write(
            messages=[*triggering_messages, assistant_msg],
        )

    def _schedule_experience_write(
        self,
        messages: List[Message],
    ) -> None:
        if not messages:
            return
        self.memory_handler.schedule_experience_write(messages)

    @staticmethod
    def _turn_message_id(user_msg: Message, iteration_index: int, suffix: str = "message") -> str:
        return f"{user_msg.timestamp:.6f}-{iteration_index}-{suffix}"

    @staticmethod
    def _message_start_event(message_id: str, phase: str) -> dict:
        return {
            "type": "message_start",
            "message_id": message_id,
            "phase": phase,
        }

    @staticmethod
    def _message_delta_event(message_id: str, phase: str, delta: str) -> dict:
        return {
            "type": "message_delta",
            "delta": delta,
            "message_id": message_id,
            "phase": phase,
        }

    @staticmethod
    def _message_done_event(
        message_id: str,
        phase: str,
        content: str,
        attachments: Optional[list[dict]] = None,
    ) -> dict:
        event = {
            "type": "message_done",
            "message_id": message_id,
            "phase": phase,
            "content": content,
        }
        if attachments:
            event["attachments"] = attachments
        return event

    @classmethod
    def _message_events(
        cls,
        message_id: str,
        phase: str,
        content: str,
        stream: bool,
        deltas: Optional[list[str]] = None,
        attachments: Optional[list[dict]] = None,
    ) -> list[dict]:
        events = [cls._message_start_event(message_id, phase)]
        if stream:
            chunks = deltas if deltas is not None else [content]
            events.extend(
                cls._message_delta_event(message_id, phase, chunk)
                for chunk in chunks
                if chunk
            )
        events.append(cls._message_done_event(message_id, phase, content, attachments=attachments))
        return events

    @staticmethod
    def _tool_event(event_type: str, tool_call: Any) -> dict:
        if isinstance(tool_call, dict):
            call_id = tool_call.get("call_id") or tool_call.get("id") or "call_0"
            name = tool_call.get("name") or ""
            function = tool_call.get("function") or {}
            if not name and isinstance(function, dict):
                name = function.get("name") or ""
            return {
                "type": event_type,
                "call_id": call_id,
                "name": name,
            }

        call_id = getattr(tool_call, "call_id", "") or getattr(tool_call, "id", "") or "call_0"
        name = getattr(tool_call, "name", "") or ""
        if not name:
            function = getattr(tool_call, "function", None)
            name = getattr(function, "name", "") if function is not None else ""
        return {
            "type": event_type,
            "call_id": call_id,
            "name": name,
        }

