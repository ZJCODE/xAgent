import json
import logging
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Union

from ..components import (
    MarkdownMemory,
    MessageStorage,
    RelationshipStore,
    SkillsStorageBase,
)
from ..integrations.langfuse import (
    NoopObservabilityRuntime,
    ObservabilityRuntime,
    build_session_id,
)
from ..schemas import (
    AgentTurnResult,
    Message,
    MessageType,
    ParticipationDecision,
    RoleType,
)
from ..tools import create_search_memory_tool, create_write_memory_tool
from .config import AgentConfig, ReplyType
from .errors import (
    ERROR_EMPTY_RESPONSE,
    ERROR_INVALID_INPUT,
    ERROR_TURN_EXHAUSTED,
    build_public_error,
    map_model_error,
)
from .inbox import (
    INBOX_KIND_METADATA_KEY,
    AgentInbox,
    InboxItem,
    InboxKind,
    normalize_inbox_kind,
)
from .handlers import MemoryHandler, MessageHandler, ModelClient
from .journal import JournalLLMService
from .providers import (
    MODEL_API_OPENAI_RESPONSES,
    PROVIDER_OPENAI,
    ReasoningConfig,
    maintenance_reasoning_config,
    model_api_uses_anthropic_client,
    normalize_model_api,
    normalize_provider_name,
)
from .tooling import ToolExecutor, ToolManager
from .working_context import (
    WorkingContextCompactor,
    WorkingContextStore,
    WorkingContextSummarizer,
    WorkingContextView,
)

logger = logging.getLogger(__name__)


class Agent:
    """AI agent runtime for a continuous agent-level message stream."""

    def __init__(
        self,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        client: Optional[Any] = None,
        model_api: str = MODEL_API_OPENAI_RESPONSES,
        model_max_tokens: Optional[int] = None,
        tools: Optional[List] = None,
        message_storage: Optional[MessageStorage] = None,
        workspace: Optional[str] = None,
        skills_storage: Optional[SkillsStorageBase] = None,
        observability: Optional[ObservabilityRuntime] = None,
        supports_vision: bool = True,
        max_history: int = AgentConfig.DEFAULT_MAX_HISTORY,
        journal_batch_size: int = AgentConfig.JOURNAL_BATCH_SIZE,
        max_iter: int = AgentConfig.DEFAULT_MAX_ITER,
        max_concurrent_tools: int = AgentConfig.DEFAULT_MAX_CONCURRENT_TOOLS,
        subconscious_activity: float = AgentConfig.SUBCONSCIOUS_ACTIVITY,
        memory_recent_days: int = AgentConfig.MEMORY_RECENT_DAYS,
        provider_name: str = PROVIDER_OPENAI,
        reasoning: Optional[ReasoningConfig] = None,
    ):
        self.model = model or AgentConfig.DEFAULT_MODEL
        self.provider_name = normalize_provider_name(provider_name) or PROVIDER_OPENAI
        self.model_api = normalize_model_api(model_api)
        self.model_max_tokens = model_max_tokens
        self.reasoning = reasoning
        self.maintenance_reasoning = maintenance_reasoning_config(reasoning)
        self.supports_vision = bool(supports_vision)
        self.max_history = max_history
        self.journal_batch_size = journal_batch_size
        self.max_iter = max_iter
        self.max_concurrent_tools = max_concurrent_tools
        self.subconscious_activity = subconscious_activity
        self.memory_recent_days = memory_recent_days
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
            self.message_storage = MessageStorage(
                path=str(self._message_storage_path(workspace_path))
            )
        else:
            self.message_storage = MessageStorage(
                path=str(self._message_storage_path(runtime_root))
            )

        # Markdown-based memory system
        if workspace_path is not None:
            memory_dir = str(self._memory_dir(workspace_path))
        else:
            memory_dir = str(self._memory_dir(runtime_root))

        self.markdown_memory = MarkdownMemory(memory_dir=memory_dir)
        self.relationship_store = RelationshipStore(
            relationships_dir=str(Path(memory_dir) / AgentConfig.RELATIONSHIPS_DIRNAME)
        )
        self.llm_service = JournalLLMService(
            client=self.client,
            model=self.model,
            provider_name=self.provider_name,
            model_api=self.model_api,
            max_tokens=self.model_max_tokens,
            reasoning=self.maintenance_reasoning,
        )
        self.memory_handler = MemoryHandler(
            memory=self.markdown_memory,
            llm_service=self.llm_service,
            message_storage=self.message_storage,
            journal_batch_size=self.journal_batch_size,
            relationship_store=self.relationship_store,
            recent_days=self.memory_recent_days,
        )
        self.working_context_compactor = self._build_working_context_compactor(
            runtime_root=runtime_root,
            workspace_path=workspace_path,
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
            provider_name=self.provider_name,
            model_api=self.model_api,
            max_tokens=self.model_max_tokens,
            reasoning=self.reasoning,
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
            observability=self.observability,
        )
        self._inbox = AgentInbox()

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

    @property
    def inbox(self) -> AgentInbox:
        current = getattr(self, "_inbox", None)
        if current is None:
            current = AgentInbox()
            self._inbox = current
        return current

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

    def _build_working_context_compactor(
        self,
        *,
        runtime_root: Path,
        workspace_path: Optional[Path],
    ) -> Optional[WorkingContextCompactor]:
        root = workspace_path or runtime_root
        store_path = root / AgentConfig.MESSAGE_DIRNAME / AgentConfig.WORKING_CONTEXT_FILENAME
        store = WorkingContextStore(store_path)
        summarizer = WorkingContextSummarizer(
            client=self.client,
            model=self.model,
            provider_name=self.provider_name,
            model_api=self.model_api,
            reasoning=self.maintenance_reasoning,
        )
        return WorkingContextCompactor(
            store=store,
            message_storage=self.message_storage,
            summarizer=summarizer,
            hot_window=self.max_history,
        )

    async def _working_context_for_turn(self) -> WorkingContextView:
        compactor = getattr(self, "working_context_compactor", None)
        if compactor is None:
            return WorkingContextView()
        try:
            view_for_turn = getattr(compactor, "view_for_turn", None)
            if callable(view_for_turn):
                view = await view_for_turn()
            else:
                view = await compactor.ensure_fresh()
            if isinstance(view, WorkingContextView):
                return view
            # Older fakes may still return a bare summary string.
            return WorkingContextView(summary=str(view or ""))
        except Exception as exc:
            logger.warning("Working context compaction failed; falling back: %s", exc)
            try:
                return await compactor.current_view()
            except Exception:
                try:
                    return WorkingContextView(summary=await compactor.current_summary())
                except Exception:
                    return WorkingContextView()

    async def _build_turn_context(
        self,
        msg_handler: MessageHandler,
        user_msg: Message,
        user_id: str,
        channel_instructions: str = "",
    ):
        """Build the shared turn preparation context for both chat and chat_events."""
        working_context = await self._working_context_for_turn()
        recent_messages = await msg_handler.get_recent_messages(
            max_history=AgentConfig.history_fetch_depth(self.max_history),
        )
        memory_context = await self.memory_handler.get_recent_context()
        relationship_context = await self._relationship_context_for_turn(
            user_msg=user_msg,
            user_id=user_id,
            recent_messages=recent_messages,
        )
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
            relationship_context=relationship_context,
            max_messages=self.max_history,
            include_images=self.supports_vision,
            workspace_dir=getattr(self, "workspace_dir", None),
            current_message=user_msg,
            channel_instructions=channel_instructions,
            working_summary=working_context.summary,
            covers_through_cursor=working_context.covers_through_cursor,
        )
        input_messages = msg_handler.sanitize_input_messages(list(iteration_messages))
        return tool_specs, instructions, iteration_messages, input_messages

    async def _relationship_context_for_turn(
        self,
        user_msg: Message,
        user_id: str,
        recent_messages: List[Message],
    ) -> str:
        """Assemble relationship cards for the current speaker and room peers."""
        memory_handler = getattr(self, "memory_handler", None)
        if memory_handler is None or not callable(
            getattr(memory_handler, "get_relationship_context", None)
        ):
            return ""

        channel = getattr(user_msg, "channel", None) or ""
        speaker_key = RelationshipStore.make_key(channel, user_id)

        participant_keys: list[str] = []
        seen = {speaker_key}
        max_peers = max(0, AgentConfig.RELATIONSHIP_MAX_CARDS_PER_TURN - 1)
        for message in reversed(recent_messages):
            if len(participant_keys) >= max_peers:
                break
            if message.type != MessageType.MESSAGE or message.role != RoleType.USER:
                continue
            peer_id = (message.sender_id or "").strip()
            if not peer_id:
                continue
            peer_key = RelationshipStore.make_key(
                (message.channel or "").strip(), peer_id
            )
            if peer_key in seen:
                continue
            seen.add(peer_key)
            participant_keys.append(peer_key)

        return await memory_handler.get_relationship_context(
            speaker_keys=[speaker_key],
            participant_keys=participant_keys,
        )

    async def run_memory_maintenance(self, trigger: str = "count") -> None:
        idle_timeout = AgentConfig.IDLE_DIARY_TIMEOUT_SECONDS
        force = False
        idle_seconds = 0.0
        if idle_timeout > 0:
            memory = getattr(self, "markdown_memory", None)
            if memory is not None:
                path = memory.root / ".last_interaction"
                try:
                    last = float(path.read_text(encoding="utf-8").strip())
                except (FileNotFoundError, ValueError):
                    last = time.time()
                elapsed = time.time() - last
                if elapsed >= idle_timeout:
                    force = True
                    trigger = "idle"
                    idle_seconds = elapsed

        await self.memory_handler.run_maintenance(
            force=force, trigger=trigger, idle_seconds=idle_seconds
        )
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
        channel_instructions: str = "",
        channel: Optional[str] = None,
    ) -> Union[str, AsyncGenerator[str, None]]:
        return await self.chat(
            user_message=user_message,
            user_id=user_id,
            image_source=image_source,
            attachments=attachments,
            stream=stream,
            channel_instructions=channel_instructions,
            channel=channel,
        )

    async def chat(
        self,
        user_message: str,
        user_id: str = AgentConfig.DEFAULT_USER_ID,
        image_source: Optional[Union[str, List[str]]] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        stream: bool = False,
        channel_instructions: str = "",
        room_name: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> Union[str, AsyncGenerator[str, None]]:
        """Generate a reply from the agent given a user message.

        Args:
            stream: When True, return an async text generator for compatibility
                with the legacy Python API. New event consumers should prefer
                ``chat_events(stream=True)``.
            room_name: Optional room/group name for multi-participant conversations.
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
                    channel_instructions=channel_instructions,
                    room_name=room_name,
                    channel=channel,
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

        final_reply = ""
        last_error = ""
        async for event in self.chat_events(
            user_message=user_message,
            user_id=user_id,
            image_source=image_source,
            attachments=attachments,
            stream=False,
            channel_instructions=channel_instructions,
            room_name=room_name,
            channel=channel,
        ):
            if event.get("type") == "message_done" and event.get("phase") == "final":
                final_reply = str(event.get("content") or "")
            elif event.get("type") == "error":
                last_error = str(event.get("error") or "")
        return final_reply or last_error

    async def chat_events(
        self,
        user_message: str,
        user_id: str = AgentConfig.DEFAULT_USER_ID,
        image_source: Optional[Union[str, List[str]]] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        stream: bool = False,
        channel_instructions: str = "",
        room_name: Optional[str] = None,
        channel: Optional[str] = None,
        inbox_kind: Optional[Union[str, InboxKind]] = None,
    ) -> AsyncGenerator[dict, None]:
        """Emit one agent turn as structured message/tool events.

        ``stream`` only controls whether text is additionally exposed as
        ``message_delta`` events. Message boundaries and tool progress are
        always eventized.

        Args:
            room_name: Optional room/group name for multi-participant conversations.
            inbox_kind: How this input should be classified. Defaults to a
                user turn; scheduled delivery context upgrades it to
                ``scheduled_turn``.
        """
        self._record_last_interaction()
        from .runtime import current_delivery_context

        delivery_context = current_delivery_context()
        resolved_channel = (channel or "").strip()
        if not resolved_channel and delivery_context is not None:
            resolved_channel = str(delivery_context.channel or "").strip()
        channel = resolved_channel or None
        inbox_item = self._inbox_item_for_chat(
            user_message=user_message,
            user_id=user_id,
            image_source=image_source,
            attachments=attachments,
            stream=stream,
            channel_instructions=channel_instructions,
            room_name=room_name,
            channel=channel,
            inbox_kind=inbox_kind,
            delivery_context=delivery_context,
        )
        user_metadata = inbox_item.message_metadata()
        await self.inbox.acquire_turn()
        try:
            async for event in self._drive_claimed_turn(
                inbox_item=inbox_item,
                user_metadata=user_metadata,
                stream=stream,
            ):
                yield event
        finally:
            self.inbox.release_turn()

    async def _drive_claimed_turn(
        self,
        *,
        inbox_item: InboxItem,
        user_metadata: Dict[str, Any],
        stream: bool,
    ) -> AsyncGenerator[dict, None]:
        """Run one claimed waking turn. Caller must hold the inbox turn lock."""
        user_message = inbox_item.content
        user_id = inbox_item.user_id
        image_source = inbox_item.image_source
        attachments = inbox_item.attachments
        channel_instructions = inbox_item.channel_instructions
        room_name = inbox_item.room_name
        channel = inbox_item.channel
        msg_handler = self.message_handler
        model_name = getattr(self, "model", AgentConfig.DEFAULT_MODEL)
        channel_name = str(channel or "local")
        session_id = build_session_id(
            channel=channel_name,
            room_name=room_name,
            user_id=user_id,
        )
        turn_ctx = self._observability_runtime().agent_turn(
            user_id=user_id,
            session_id=session_id,
            model=model_name,
            channel=channel_name,
            stream=stream,
        )
        with turn_ctx as turn_obs:
            try:
                user_msg = await msg_handler.store_user_message(
                    user_message,
                    user_id,
                    image_source,
                    attachments=attachments,
                    room_name=room_name,
                    channel=channel,
                    metadata=user_metadata,
                )
            except ValueError as exc:
                payload = build_public_error(
                    code=ERROR_INVALID_INPUT,
                    message=str(exc),
                    cause=exc,
                )
                turn_obs.set_error(
                    error_id=payload["error_id"],
                    code=payload["error_code"],
                    message=payload["error"],
                )
                yield payload
                yield {"type": "done"}
                return

            tool_specs, instructions, iteration_messages, input_messages = await self._build_turn_context(
                msg_handler=msg_handler,
                user_msg=user_msg,
                user_id=user_id,
                channel_instructions=channel_instructions,
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
                        payload = build_public_error(
                            code=map_model_error(model_event.error),
                            cause=model_event.error,
                        )
                        turn_obs.set_error(
                            error_id=payload["error_id"],
                            code=payload["error_code"],
                            message=payload["error"],
                        )
                        yield payload
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
                            room_name=room_name,
                            channel=channel,
                            recipient_id=room_name or user_id,
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
                            room_name=room_name,
                            channel=channel,
                            recipient_id=room_name or user_id,
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
                        room_name=room_name,
                        channel=channel,
                        recipient_id=room_name or user_id,
                    )
                    self._schedule_experience_write(
                        messages=[user_msg, assistant_msg],
                    )
                    turn_obs.set_output(visible_text)
                    yield {"type": "done"}
                    return

                payload = build_public_error(
                    code=ERROR_EMPTY_RESPONSE,
                    cause="Model stream ended without text or tool calls",
                )
                turn_obs.set_error(
                    error_id=payload["error_id"],
                    code=payload["error_code"],
                    message=payload["error"],
                )
                yield payload
                yield {"type": "done"}
                return

            payload = build_public_error(
                code=ERROR_TURN_EXHAUSTED,
                cause=f"Failed to generate response after {self.max_iter} attempts",
            )
            turn_obs.set_error(
                error_id=payload["error_id"],
                code=payload["error_code"],
                message=payload["error"],
            )
            yield payload
            yield {"type": "done"}

        # Flush after turn context exits
        try:
            await self._observability_runtime().flush()
        except Exception as exc:
            logger.warning("Failed to flush observability events: %s", exc)

    def _inbox_item_for_chat(
        self,
        *,
        user_message: str,
        user_id: str,
        image_source: Optional[Union[str, List[str]]],
        attachments: Optional[List[Dict[str, Any]]],
        stream: bool,
        channel_instructions: str,
        room_name: Optional[str],
        channel: Optional[str],
        inbox_kind: Optional[Union[str, InboxKind]],
        delivery_context: Any,
    ) -> InboxItem:
        kind = normalize_inbox_kind(inbox_kind)
        extra_metadata: Dict[str, Any] = {}
        if delivery_context is not None:
            source = str((delivery_context.metadata or {}).get("source") or "").strip()
            if source:
                extra_metadata["source"] = source
            if source == "scheduled_task" and kind is InboxKind.USER_TURN:
                kind = InboxKind.SCHEDULED_TURN
        return InboxItem(
            kind=kind,
            content=user_message,
            user_id=user_id,
            channel=channel,
            room_name=room_name,
            attachments=attachments,
            image_source=image_source,
            channel_instructions=channel_instructions,
            metadata=extra_metadata,
            stream=stream,
        )

    async def submit(self, item: InboxItem):
        """Submit a classified inbox item.

        Observations persist without waking a model turn. Waking items are
        driven through ``chat_events``.
        """
        if item.kind is InboxKind.OBSERVATION:
            return await self.observe(
                context=item.content,
                source=str((item.metadata or {}).get("source") or "environment"),
                event_type=str((item.metadata or {}).get("event_type") or "observation"),
                metadata=item.metadata,
                room_name=item.room_name,
                channel=item.channel,
            )
        return self.chat_events(
            user_message=item.content,
            user_id=item.user_id,
            image_source=item.image_source,
            attachments=item.attachments,
            stream=item.stream,
            channel_instructions=item.channel_instructions,
            room_name=item.room_name,
            channel=item.channel,
            inbox_kind=item.kind,
        )

    async def observe(
        self,
        context: str,
        source: str = "environment",
        event_type: str = "observation",
        metadata: Optional[Dict[str, Any]] = None,
        room_name: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> AgentTurnResult:
        """Record environmental context without generating a reply."""
        event_metadata = dict(metadata or {})
        event_metadata.setdefault(INBOX_KIND_METADATA_KEY, InboxKind.OBSERVATION.value)
        event_msg = await self.message_handler.store_context_event(
            context=context,
            source=source,
            event_type=event_type,
            metadata=event_metadata,
            room_name=room_name,
            channel=channel,
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

    async def record_subconscious_thought(
        self,
        content: str,
    ) -> AgentTurnResult:
        """Record a raw subconscious thought directly in the diary."""
        note = str(content or "").strip()
        if note:
            await self.markdown_memory.append_daily(note)
        return AgentTurnResult(
            kind="subconscious_thought",
            replied=False,
            reply=None,
            event_id=time.time(),
            event_type="subconscious_thought",
            source="subconscious",
        )

    async def decide_participation(
        self,
        context: str,
        source: str = "environment",
        event_type: str = "observation",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ParticipationDecision:
        """Decide whether an observed event deserves an outward reply.

        Uses only the provided room context (which should include recent group
        history) and the agent's identity. Does not pull from message storage
        or memory — the decision is scoped to the current room's conversation.
        """
        try:
            instructions = [{
                "role": "system",
                "name": AgentConfig.DECISION_RULES_NAME,
                "content": AgentConfig.DECISION_SYSTEM_PROMPT,
            }]
            if self.system_prompt.strip():
                instructions.append({
                    "role": "system",
                    "name": AgentConfig.IDENTITY_CONTEXT_NAME,
                    "content": AgentConfig.build_identity_context(self.system_prompt),
                })

            input_messages = [{
                "role": "user",
                "name": "participation_decision",
                "content": self._build_participation_decision_prompt(
                    context=context,
                    source=source,
                    event_type=event_type,
                ),
            }]

            reply_type, payload = await self.model_client.call(
                messages=input_messages,
                tool_specs=None,
                instructions=instructions,
                stream=False,
            )
            if reply_type == ReplyType.SIMPLE_REPLY:
                return self._parse_participation_decision(str(payload))
            logger.warning("Participation decision returned non-text result: %s", reply_type)
        except Exception as exc:
            logger.warning("Participation decision failed: %s", exc, exc_info=True)
        return ParticipationDecision(should_reply=False, reason="participation decision failed")

    @staticmethod
    def _build_participation_decision_prompt(
        *,
        context: str,
        source: str,
        event_type: str,
    ) -> str:
        return (
            "<participation_decision>\n"
            f"Source: {source}\n"
            f"Event type: {event_type}\n\n"
            "Recent group conversation:\n"
            f"{context.strip()}\n\n"
            "Decide whether to reply now. Prefer joining when you have something to add. "
            "Return JSON only:\n"
            '{"should_reply": true|false, "reason": "brief reason"}\n'
            "</participation_decision>"
        )

    @staticmethod
    def _parse_participation_decision(payload: str) -> ParticipationDecision:
        text = str(payload or "").strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                try:
                    data = json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    data = {}
            else:
                data = {}

        if not isinstance(data, dict):
            data = {}
        return ParticipationDecision(
            should_reply=bool(data.get("should_reply")),
            reason=str(data.get("reason") or "").strip() or None,
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
        room_name = next((m.room_name for m in triggering_messages if m.room_name), None)
        channel = next((m.channel for m in triggering_messages if m.channel), None)
        recipient_id = room_name or next(
            (m.sender_id for m in triggering_messages if m.sender_id and m.role == RoleType.USER), None
        )
        assistant_msg = await msg_handler.store_model_reply(
            reply_text,
            self._assistant_sender_id,
            room_name=room_name,
            channel=channel,
            recipient_id=recipient_id,
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
