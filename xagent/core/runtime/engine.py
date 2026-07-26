"""Single-writer cognitive runtime for one xAgent individual."""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from typing import Any, Protocol

from .state import RuntimeStateStore
from .delivery_context import DeliveryContext, current_delivery_context, delivery_context
from .types import (
    DELIVERY_CHANNELS,
    EVENT_STATUS_COMPLETED,
    EVENT_STATUS_FAILED,
    EVENT_STATUS_NEEDS_REVIEW,
    AgentEvent,
    Delivery,
    StoredEvent,
)


logger = logging.getLogger(__name__)
DEFAULT_TURN_TIMEOUT_SECONDS = 600.0
_MUTATING_TOOLS = {
    "run_command",
    "write_memory",
    "manage_scheduled_tasks",
    "attach_artifact",
    "generate_image",
}


class ChannelAdapter(Protocol):
    name: str

    async def run(
        self,
        emit: Callable[[AgentEvent], Awaitable[None]],
    ) -> None: ...

    async def send(self, delivery: Delivery) -> None: ...

    async def stop(self) -> None: ...


class AgentRuntime:
    """Persist and process every cognitive event in one total order."""

    def __init__(
        self,
        agent: Any,
        state: RuntimeStateStore,
        *,
        turn_timeout_seconds: float = DEFAULT_TURN_TIMEOUT_SECONDS,
    ) -> None:
        self.agent = agent
        self.state = state
        self.turn_timeout_seconds = max(1.0, float(turn_timeout_seconds))
        self._stop_event = asyncio.Event()
        self._wakeup = asyncio.Event()
        self._actor_task: asyncio.Task[None] | None = None
        self.cognitive_lock = asyncio.Lock()
        self._waiters: dict[str, list[asyncio.Future[dict[str, Any]]]] = defaultdict(list)
        self._streams: dict[str, list[asyncio.Queue[dict[str, Any]]]] = defaultdict(list)
        self._subconscious_handler: Callable[[AgentEvent], Awaitable[dict[str, Any]]] | None = None

    def set_subconscious_handler(
        self,
        handler: Callable[[AgentEvent], Awaitable[dict[str, Any]]],
    ) -> None:
        self._subconscious_handler = handler

    @property
    def running(self) -> bool:
        return self._actor_task is not None and not self._actor_task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        await self.state.recover_interrupted()
        self._actor_task = asyncio.create_task(self._actor_loop(), name="xagent-cognitive-actor")
        self._wakeup.set()

    async def stop(self) -> None:
        self._stop_event.set()
        self._wakeup.set()
        task = self._actor_task
        if task is None:
            return
        try:
            await asyncio.wait_for(task, timeout=self.turn_timeout_seconds + 5.0)
        except asyncio.TimeoutError:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        finally:
            self._actor_task = None

    async def submit(self, event: AgentEvent) -> str:
        await self.state.enqueue_event(event)
        self._wakeup.set()
        return event.event_id

    async def submit_and_wait(self, event: AgentEvent) -> dict[str, Any]:
        if not self.running:
            raise RuntimeError("agent runtime is not running")
        existing = await self.state.get_event(event.event_id)
        if existing is not None:
            await self.state.enqueue_event(event)
            if existing.status == EVENT_STATUS_COMPLETED:
                return dict(existing.result or {})
            if existing.status in {
                EVENT_STATUS_FAILED,
                EVENT_STATUS_NEEDS_REVIEW,
            }:
                raise RuntimeError(existing.error or existing.status)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._waiters[event.event_id].append(future)
        try:
            await self.submit(event)
            return await future
        finally:
            waiters = self._waiters.get(event.event_id)
            if waiters is not None:
                with suppress(ValueError):
                    waiters.remove(future)
                if not waiters:
                    self._waiters.pop(event.event_id, None)

    async def stream(self, event: AgentEvent) -> AsyncIterator[dict[str, Any]]:
        if not self.running:
            raise RuntimeError("agent runtime is not running")
        existing = await self.state.get_event(event.event_id)
        if existing is not None:
            await self.state.enqueue_event(event)
            if existing.status == EVENT_STATUS_COMPLETED:
                result = dict(existing.result or {})
                for item in result.get("events", []):
                    yield dict(item)
                if not result.get("events") or result["events"][-1].get("type") != "done":
                    yield {"type": "done"}
                return
            if existing.status in {
                EVENT_STATUS_FAILED,
                EVENT_STATUS_NEEDS_REVIEW,
            }:
                yield {"type": "error", "error": existing.error or existing.status}
                yield {"type": "done"}
                return
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._streams[event.event_id].append(queue)
        try:
            await self.submit(event)
            while True:
                item = await queue.get()
                yield item
                if item.get("type") == "done":
                    return
        finally:
            streams = self._streams.get(event.event_id)
            if streams is not None:
                with suppress(ValueError):
                    streams.remove(queue)
                if not streams:
                    self._streams.pop(event.event_id, None)

    async def run_forever(self) -> None:
        await self.start()
        assert self._actor_task is not None
        await self._actor_task

    async def _actor_loop(self) -> None:
        while True:
            stored = await self.state.claim_next_event()
            if stored is None:
                if self._stop_event.is_set():
                    return
                self._wakeup.clear()
                try:
                    await asyncio.wait_for(self._wakeup.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                continue
            await self._process(stored)

    async def _process(self, stored: StoredEvent) -> None:
        async with self.cognitive_lock:
            await self._process_locked(stored)

    async def _process_locked(self, stored: StoredEvent) -> None:
        event = stored.event
        tool_executor = getattr(self.agent, "tool_executor", None)
        old_callback = getattr(tool_executor, "before_execute", None)

        async def before_execute(tool_name: str) -> None:
            if tool_name in _MUTATING_TOOLS:
                await self.state.mark_side_effect_started(event.event_id)

        if tool_executor is not None:
            tool_executor.before_execute = before_execute
        try:
            result = await asyncio.wait_for(
                self._dispatch_and_maintain(event),
                timeout=self.turn_timeout_seconds,
            )
            await self.state.complete_event(event.event_id, result)
            self._resolve_waiters(event.event_id, result=result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Runtime event failed event_id=%s", event.event_id)
            await self.state.fail_event(event.event_id, str(exc))
            self._resolve_waiters(event.event_id, error=exc)
            await self._publish(event.event_id, {"type": "error", "error": str(exc)})
            await self._publish(event.event_id, {"type": "done"})
        finally:
            if tool_executor is not None:
                tool_executor.before_execute = old_callback

    async def _dispatch_and_maintain(self, event: AgentEvent) -> dict[str, Any]:
        result = await self._dispatch_event(event)
        maintenance = getattr(self.agent, "run_memory_maintenance", None)
        if callable(maintenance):
            await self.state.mark_side_effect_started(event.event_id)
            try:
                await maintenance(trigger="count")
            except Exception:
                logger.warning(
                    "Serialized diary maintenance failed event_id=%s",
                    event.event_id,
                    exc_info=True,
                )
        return result

    async def _dispatch_event(self, event: AgentEvent) -> dict[str, Any]:
        metadata = dict(event.metadata)
        if event.kind == "chat":
            emitted: list[dict[str, Any]] = []
            persisted_context = metadata.get("delivery_context")
            context = (
                DeliveryContext(
                    source=str(persisted_context.get("source") or event.source),
                    channel=(
                        str(persisted_context["channel"])
                        if persisted_context.get("channel") in DELIVERY_CHANNELS
                        else None
                    ),
                    user_id=str(persisted_context.get("user_id") or event.speaker_id),
                    target=dict(persisted_context.get("target") or {}),
                    metadata=dict(persisted_context.get("metadata") or {}),
                )
                if isinstance(persisted_context, dict)
                else DeliveryContext(
                    source=event.source,
                    channel=event.source if event.source in DELIVERY_CHANNELS else None,
                    user_id=event.speaker_id,
                )
            )
            with delivery_context(context):
                conversation = (
                    event.conversation_id.replace("\n", " ").replace("\r", " ")[:120]
                    or "[direct]"
                )
                visible_audience = list(event.audience_ids[:4])
                audience = ",".join(visible_audience) or event.speaker_id
                if len(event.audience_ids) > len(visible_audience):
                    audience += f",+{len(event.audience_ids) - len(visible_audience)}"
                scope = (
                    f"source={event.source}\n"
                    f"conversation={conversation}\n"
                    f"audience={audience}"
                )
                supplied = str(metadata.get("channel_instructions") or "").strip()
                channel_instructions = (
                    scope if not supplied else f"{scope}\n{supplied}"
                )
                async for item in self.agent.chat_events(
                    user_message=event.content,
                    user_id=event.speaker_id,
                    image_source=metadata.get("image_source"),
                    attachments=metadata.get("attachments"),
                    stream=bool(metadata.get("stream", False)),
                    channel_instructions=channel_instructions,
                    room_name=metadata.get("room_name"),
                    source=event.source,
                    runtime_event_id=event.event_id,
                ):
                    payload = dict(item)
                    emitted.append(payload)
                    if payload.get("type") in {
                        "message_start",
                        "message_delta",
                        "message_done",
                    }:
                        await self.state.mark_side_effect_started(event.event_id)
                    await self._publish(event.event_id, payload)
            if not emitted or emitted[-1].get("type") != "done":
                await self._publish(event.event_id, {"type": "done"})
            return {"kind": "chat", "events": emitted}

        if event.kind == "observe":
            value = await self.agent.observe(
                context=event.content,
                source=str(metadata.get("source") or event.source),
                event_type=str(metadata.get("event_type") or "observation"),
                metadata=metadata.get("observation_metadata"),
                room_name=metadata.get("room_name"),
                event_source=event.source,
                runtime_event_id=event.event_id,
            )
            result = value.model_dump(mode="json") if hasattr(value, "model_dump") else dict(value)
            await self._publish(event.event_id, {"type": "result", "result": result})
            await self._publish(event.event_id, {"type": "done"})
            return result

        if event.kind == "subconscious":
            if self._subconscious_handler is None:
                raise RuntimeError("subconscious processing is not configured")
            result = await self._subconscious_handler(event)
            await self._publish(event.event_id, {"type": "result", "result": result})
            await self._publish(event.event_id, {"type": "done"})
            return result

        if event.kind == "participation":
            value = await self.agent.decide_participation(
                context=event.content,
                source=str(metadata.get("source") or event.source),
                event_type=str(metadata.get("event_type") or "observation"),
                metadata=metadata.get("observation_metadata"),
            )
            result = value.model_dump(mode="json") if hasattr(value, "model_dump") else dict(value)
            await self._publish(event.event_id, {"type": "result", "result": result})
            await self._publish(event.event_id, {"type": "done"})
            return result

        raise ValueError(f"unsupported runtime event kind: {event.kind}")

    async def _publish(self, event_id: str, payload: dict[str, Any]) -> None:
        for queue in list(self._streams.get(event_id, ())):
            await queue.put(dict(payload))

    def _resolve_waiters(
        self,
        event_id: str,
        *,
        result: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        for future in list(self._waiters.pop(event_id, ())):
            if future.done():
                continue
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(dict(result or {}))


class RuntimeAgentProxy:
    """Agent-shaped facade routing channel cognition through AgentRuntime."""

    def __init__(self, runtime: AgentRuntime) -> None:
        self.runtime = runtime

    @property
    def workspace_dir(self) -> Any:
        """Expose only the read-only path adapters need for media transport."""
        return self.runtime.agent.workspace_dir

    @property
    def supports_vision(self) -> bool:
        return bool(self.runtime.agent.supports_vision)

    async def chat_events(
        self,
        *,
        user_message: str,
        user_id: str,
        image_source: Any = None,
        attachments: Any = None,
        stream: bool = False,
        channel_instructions: str = "",
        room_name: str | None = None,
        source: str | None = None,
        event_id: str | None = None,
        conversation_id: str | None = None,
        audience_ids: tuple[str, ...] | list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        resolved_source = source or "unknown"
        person_id = await self.runtime.state.resolve_person(resolved_source, user_id)
        context = current_delivery_context()
        persisted_context = {
            "source": context.source,
            "channel": context.channel,
            "user_id": context.user_id,
            "target": dict(context.target),
            "metadata": dict(context.metadata),
        } if context is not None else {
            "source": resolved_source,
            "channel": (
                resolved_source if resolved_source in DELIVERY_CHANNELS else None
            ),
            "user_id": user_id,
            "target": {"user_id": user_id},
            "metadata": {},
        }
        persisted_context["metadata"]["person_id"] = person_id
        event = AgentEvent.create(
            event_id=event_id,
            kind="chat",
            source=resolved_source,
            speaker_id=person_id,
            conversation_id=conversation_id or f"{resolved_source}:{room_name or user_id}",
            audience_ids=tuple(audience_ids) if audience_ids else (person_id,),
            content=user_message,
            metadata={
                "image_source": image_source,
                "attachments": attachments,
                "stream": stream,
                "channel_instructions": channel_instructions,
                "room_name": room_name,
                "channel_account_id": user_id,
                "delivery_context": persisted_context,
            },
        )
        async for item in self.runtime.stream(event):
            yield item

    async def chat(self, **kwargs: Any) -> str:
        final = ""
        error = ""
        async for item in self.chat_events(**kwargs):
            if item.get("type") == "message_done" and item.get("phase") == "final":
                final = str(item.get("content") or "")
            elif item.get("type") == "error":
                error = str(item.get("error") or "")
        return final or error

    async def __call__(self, **kwargs: Any) -> str:
        return await self.chat(**kwargs)

    async def observe(
        self,
        context: str,
        source: str = "environment",
        event_type: str = "observation",
        metadata: dict[str, Any] | None = None,
        room_name: str | None = None,
        event_source: str | None = None,
    ) -> Any:
        event_metadata = dict(metadata or {})
        resolved_source = event_source or source or "environment"
        account_id = str(
            event_metadata.get("sender_id")
            or event_metadata.get("account_id")
            or source
            or "environment"
        )
        person_id = await self.runtime.state.resolve_person(resolved_source, account_id)
        event = AgentEvent.create(
            kind="observe",
            source=resolved_source,
            speaker_id=person_id,
            conversation_id=str(event_metadata.get("chat_id") or room_name or ""),
            content=context,
            metadata={
                "source": source,
                "event_type": event_type,
                "channel_account_id": account_id,
                "observation_metadata": event_metadata,
                "room_name": room_name,
            },
        )
        result = await self.runtime.submit_and_wait(event)
        from ...schemas import AgentTurnResult

        return AgentTurnResult.model_validate(result)

    async def decide_participation(
        self,
        context: str,
        source: str = "environment",
        event_type: str = "observation",
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        event_metadata = dict(metadata or {})
        resolved_source = str(event_metadata.get("channel") or source or "environment")
        account_id = str(
            event_metadata.get("sender_id")
            or event_metadata.get("account_id")
            or source
            or "environment"
        )
        person_id = await self.runtime.state.resolve_person(resolved_source, account_id)
        event = AgentEvent.create(
            kind="participation",
            source=resolved_source,
            speaker_id=person_id,
            conversation_id=str(
                event_metadata.get("chat_id")
                or event_metadata.get("room_name")
                or ""
            ),
            audience_ids=(
                (f"{resolved_source}-room:{event_metadata['chat_id']}",)
                if event_metadata.get("chat_id")
                else (person_id,)
            ),
            content=context,
            metadata={
                "source": source,
                "event_type": event_type,
                "channel_account_id": account_id,
                "observation_metadata": event_metadata,
            },
        )
        result = await self.runtime.submit_and_wait(event)
        from ...schemas import ParticipationDecision

        return ParticipationDecision.model_validate(result)
