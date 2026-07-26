import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Union

from ..components import (
    MarkdownMemory,
    MessageStorage,
    SkillsStorageBase,
)
from .journal import JournalLLMService
from ..integrations.langfuse import NoopObservabilityRuntime, ObservabilityRuntime
from .config import AgentConfig, ReplyType
from .prompts import PromptAssembler
from .handlers import MemoryHandler, MessageHandler, ModelClient
from .providers import (
    PROVIDER_OPENAI,
    ReasoningConfig,
    normalize_model_api,
    normalize_provider_name,
)
from .tooling import ToolExecutor, ToolManager
from ..schemas import AgentTurnResult, Message, MessageType, ParticipationDecision, RoleType
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentDependencies:
    """Fully assembled dependencies; construction performs no I/O."""

    client: Any
    message_storage: MessageStorage
    markdown_memory: MarkdownMemory
    llm_service: JournalLLMService
    memory_handler: MemoryHandler
    message_handler: MessageHandler
    tool_manager: ToolManager
    model_client: ModelClient
    tool_executor: ToolExecutor
    workspace_dir: Path
    skills_storage: Optional[SkillsStorageBase]
    observability: ObservabilityRuntime


class Agent:
    """AI agent runtime for a continuous agent-level message stream."""

    def __init__(
        self,
        *,
        identity: str,
        model: str,
        dependencies: AgentDependencies,
        model_api: str,
        model_max_tokens: Optional[int],
        provider_name: str,
        reasoning: Optional[ReasoningConfig],
        supports_vision: bool = True,
        max_history: int = AgentConfig.DEFAULT_MAX_HISTORY,
        max_iter: int = AgentConfig.DEFAULT_MAX_ITER,
        subconscious_activity: float = AgentConfig.SUBCONSCIOUS_ACTIVITY,
        memory_recent_days: int = AgentConfig.MEMORY_RECENT_DAYS,
    ):
        self.model = model
        self.provider_name = normalize_provider_name(provider_name) or PROVIDER_OPENAI
        self.model_api = normalize_model_api(model_api)
        self.model_max_tokens = model_max_tokens
        self.reasoning = reasoning
        self.supports_vision = bool(supports_vision)
        self.max_history = max_history
        self.max_iter = max_iter
        self.subconscious_activity = subconscious_activity
        self.memory_recent_days = memory_recent_days
        self.observability = dependencies.observability
        self.client = dependencies.client
        self.system_prompt = identity
        self._assistant_sender_id = "agent"
        self.workspace_dir = dependencies.workspace_dir
        self.skills_storage = dependencies.skills_storage
        self.message_storage = dependencies.message_storage
        self.markdown_memory = dependencies.markdown_memory
        self.llm_service = dependencies.llm_service
        self.memory_handler = dependencies.memory_handler
        self.tool_manager = dependencies.tool_manager
        self.model_client = dependencies.model_client
        self.message_handler = dependencies.message_handler
        self.tool_executor = dependencies.tool_executor

    @property
    def identity(self) -> str:
        return self.system_prompt

    @property
    def tools(self) -> dict:
        return self.tool_manager.tools

    def _skills_catalog_context(self) -> str:
        skills_storage = getattr(self, "skills_storage", None)
        if skills_storage is None:
            return ""
        return skills_storage.catalog_text(max_chars=AgentConfig.MAX_SKILLS_CATALOG_CHARS)

    def _workspace_context(self, tool_names: List[str]) -> str:
        if "run_command" not in tool_names:
            return ""
        return PromptAssembler.workspace_context(str(self.workspace_dir))

    async def _build_turn_context(
        self,
        msg_handler: MessageHandler,
        user_msg: Message,
        user_id: str,
        channel_instructions: str = "",
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
            memory_recent_days=getattr(self, "memory_recent_days", AgentConfig.MEMORY_RECENT_DAYS),
        )
        iteration_messages = msg_handler.build_turn_context_messages(
            recent_messages,
            current_user_id=user_id,
            memory_context=memory_context,
            max_messages=self.max_history,
            include_images=self.supports_vision,
            workspace_dir=getattr(self, "workspace_dir", None),
            current_message=user_msg,
            channel_instructions=channel_instructions,
        )
        instructions, iteration_messages = PromptAssembler.apply_budget(
            instructions,
            iteration_messages,
            tool_specs,
        )
        input_messages = msg_handler.sanitize_input_messages(list(iteration_messages))
        return tool_specs, instructions, iteration_messages, input_messages

    async def run_memory_maintenance(self, trigger: str = "count") -> None:
        idle_timeout = AgentConfig.IDLE_DIARY_TIMEOUT_SECONDS
        force = False
        idle_seconds = 0.0
        if idle_timeout > 0:
            try:
                last = float(
                    self.message_storage.get_journal_state_sync(
                        "last_interaction",
                        str(time.time()),
                    )
                )
            except ValueError:
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
        source: Optional[str] = None,
    ) -> Union[str, AsyncGenerator[str, None]]:
        return await self.chat(
            user_message=user_message,
            user_id=user_id,
            image_source=image_source,
            attachments=attachments,
            stream=stream,
            channel_instructions=channel_instructions,
            source=source,
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
        source: Optional[str] = None,
        runtime_event_id: Optional[str] = None,
    ) -> Union[str, AsyncGenerator[str, None]]:
        """Generate a reply from the agent given a user message.

        Args:
            stream: When True, return an async text generator.
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
                    source=source,
                    runtime_event_id=runtime_event_id,
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
            source=source,
            runtime_event_id=runtime_event_id,
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
        source: Optional[str] = None,
        runtime_event_id: Optional[str] = None,
    ) -> AsyncGenerator[dict, None]:
        """Emit one agent turn as structured message/tool events.

        ``stream`` only controls whether text is additionally exposed as
        ``message_delta`` events. Message boundaries and tool progress are
        always eventized.

        Args:
            room_name: Optional room/group name for multi-participant conversations.
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
                    room_name=room_name,
                    source=source,
                    runtime_event_id=runtime_event_id,
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
                            room_name=room_name,
                            source=source,
                            recipient_id=room_name or user_id,
                        )

                    for tool_call in tool_calls:
                        yield self._tool_event("tool_call", tool_call)

                    tool_result = await self.tool_executor.handle_tool_calls(
                        tool_calls,
                        iteration_messages,
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
                        await msg_handler.store_model_reply(
                            tool_result.description,
                            self._assistant_sender_id,
                            metadata={"turn_phase": "final"},
                            attachments=tool_result.attachments,
                            room_name=room_name,
                            source=source,
                            recipient_id=room_name or user_id,
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
                    await msg_handler.store_model_reply(
                        visible_text,
                        self._assistant_sender_id,
                        metadata={"turn_phase": "final"},
                        room_name=room_name,
                        source=source,
                        recipient_id=room_name or user_id,
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
        room_name: Optional[str] = None,
        event_source: Optional[str] = None,
        runtime_event_id: Optional[str] = None,
    ) -> AgentTurnResult:
        """Record environmental context without generating a reply."""
        event_msg = await self.message_handler.store_context_event(
            context=context,
            source=source,
            event_type=event_type,
            metadata=metadata,
            room_name=room_name,
            event_source=event_source,
            runtime_event_id=runtime_event_id,
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
                "content": PromptAssembler.core_contract(
                    supports_vision=False,
                ),
            }]
            if self.system_prompt.strip():
                instructions.append({
                    "role": "system",
                    "name": AgentConfig.IDENTITY_CONTEXT_NAME,
                    "content": PromptAssembler.identity_context(self.system_prompt),
                })

            input_messages = [{
                "role": "user",
                "name": "participation_decision",
                "content": PromptAssembler.participation_task(
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
        """Write the current timestamp to unified SQLite state."""
        try:
            self.message_storage.set_journal_state_sync(
                "last_interaction",
                str(time.time()),
            )
        except OSError:
            pass

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
