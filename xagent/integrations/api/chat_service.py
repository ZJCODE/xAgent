"""Chat and observe execution for the api channel."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import aclosing, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import HTTPException, WebSocket
from starlette.websockets import WebSocketDisconnect

from ...core.agent import Agent
from ...core.runtime import ScheduledDeliveryContext, scheduled_delivery_context, upsert_contact
from ...interfaces.server.models import AgentInput, ChatInput, ObserveInput
from ...interfaces.server.serializers import response_payload
from .config import ChatLimits
from .constants import CHANNEL_API, CLIENT_HTTP, CLIENT_WS
from .input_normalization import input_attachments, input_image_sources

CancelReason = Literal["cancel", "disconnect"]


@dataclass
class _ChatStreamState:
    """Tracks the in-flight assistant message so cancel can close it cleanly."""

    open_message_id: Optional[str] = None
    open_phase: Optional[str] = None
    content_parts: list[str] = field(default_factory=list)
    done_sent: bool = False

    def observe(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "message_start":
            self.open_message_id = event.get("message_id")
            self.open_phase = event.get("phase")
            self.content_parts = []
            return
        if event_type == "message_delta":
            delta = event.get("delta")
            if delta:
                self.content_parts.append(str(delta))
            if self.open_message_id is None and event.get("message_id"):
                self.open_message_id = event.get("message_id")
                self.open_phase = event.get("phase")
            return
        if event_type == "message_done":
            self.open_message_id = None
            self.open_phase = None
            self.content_parts = []
            return
        if event_type == "done":
            self.done_sent = True


class ChatService:
    """Owns chat concurrency limits and agent turn execution."""

    def __init__(
        self,
        agent: Agent,
        *,
        contacts_file: Path,
        limits: ChatLimits,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.agent = agent
        self.contacts_file = contacts_file
        self.limits = limits
        self.logger = logger or logging.getLogger(self.__class__.__name__)
        self._semaphore = asyncio.Semaphore(max(1, int(limits.max_concurrent_chats)))
        self._queue_timeout = max(0.001, float(limits.chat_queue_timeout))
        self._chat_timeout = max(0.001, float(limits.chat_timeout))

    async def run_chat(self, input_data: ChatInput, *, client: str = CLIENT_HTTP) -> Any:
        await self._acquire_slot()
        try:
            deadline = time.monotonic() + self._chat_timeout
            return await self._await_before_deadline(
                self._call_agent(input_data, client=client),
                deadline,
            )
        except asyncio.TimeoutError as exc:
            raise HTTPException(status_code=504, detail="Agent chat timed out.") from exc
        finally:
            self._semaphore.release()

    async def run_observe(self, input_data: ObserveInput) -> Any:
        await self._acquire_slot()
        try:
            deadline = time.monotonic() + self._chat_timeout
            return await self._await_before_deadline(self._call_observe(input_data), deadline)
        except asyncio.TimeoutError as exc:
            raise HTTPException(status_code=504, detail="Agent observe timed out.") from exc
        finally:
            self._semaphore.release()

    async def send_websocket_chat_events(
        self,
        websocket: WebSocket,
        input_data: AgentInput,
        *,
        client: str = CLIENT_WS,
    ) -> None:
        """Stream one chat turn and cancel cooperatively on client stop/disconnect.

        Control plane:
        - Client may send ``{"type": "cancel"}`` to stop the active turn.
        - Client disconnect also cancels the turn (defense in depth).

        On graceful cancel the server finalizes any open message with
        ``phase=cancelled`` and emits ``{"type": "done", "reason": "cancelled"}``.
        """
        state = _ChatStreamState()
        forward_task = asyncio.create_task(
            self._forward_chat_events(websocket, input_data, client=client, state=state),
            name="ws-chat-forward",
        )
        cancel_task = asyncio.create_task(
            self._wait_for_chat_cancel(websocket),
            name="ws-chat-cancel-waiter",
        )

        done, _pending = await asyncio.wait(
            {forward_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if cancel_task in done and not forward_task.done():
            reason = cancel_task.result()
            await self._cancel_task(forward_task)
            if reason == "disconnect":
                self.logger.info(
                    "WebSocket chat cancelled by disconnect for %s",
                    input_data.user_id,
                )
                raise WebSocketDisconnect()
            self.logger.info("WebSocket chat cancelled by client for %s", input_data.user_id)
            await self._send_cancelled_terminal(websocket, state)
            return

        await self._cancel_task(cancel_task)
        # Propagate forward-task errors (timeouts/errors already yielded as events).
        await forward_task

    async def send_websocket_observe_events(self, websocket: WebSocket, input_data: ObserveInput) -> None:
        try:
            response = await self.run_observe(input_data)
            await websocket.send_json({
                "type": "result",
                "result": response_payload(response),
            })
        except HTTPException as exc:
            self.logger.warning(
                "WebSocket observe rejected: source=%s type=%s detail=%s",
                input_data.source,
                input_data.event_type,
                exc.detail,
            )
            await websocket.send_json({
                "type": "error",
                "error": exc.detail,
                "status_code": exc.status_code,
            })
        except Exception as exc:
            self.logger.error(
                "WebSocket observe error: source=%s type=%s error=%s",
                input_data.source,
                input_data.event_type,
                exc,
            )
            await websocket.send_json({
                "type": "error",
                "error": f"Agent observe error: {str(exc)}",
            })
        finally:
            await websocket.send_json({"type": "done"})

    @staticmethod
    async def send_websocket_error(
        websocket: WebSocket,
        error: str,
        *,
        status_code: Optional[int] = None,
        details: Optional[Any] = None,
    ) -> None:
        payload: dict[str, Any] = {"type": "error", "error": error}
        if status_code is not None:
            payload["status_code"] = status_code
        if details is not None:
            payload["details"] = details
        await websocket.send_json(payload)
        await websocket.send_json({"type": "done"})

    async def chat_event_stream(self, input_data: AgentInput, *, client: str = CLIENT_WS):
        acquired = False
        done_sent = False
        try:
            await self._acquire_slot()
            acquired = True
            deadline = time.monotonic() + self._chat_timeout

            chat_events = getattr(self.agent, "chat_events", None)
            if not callable(chat_events):
                raise RuntimeError("Agent does not support chat_events().")
            attachments = input_attachments(input_data)
            self._record_contact(input_data.user_id)
            context = self._scheduled_delivery_context(input_data, client=client)
            with scheduled_delivery_context(context):
                response = chat_events(
                    user_message=input_data.user_message,
                    user_id=input_data.user_id,
                    image_source=input_image_sources(input_data, attachments=attachments),
                    attachments=attachments,
                    stream=bool(input_data.stream),
                    channel=CHANNEL_API,
                )
                async for event in self._iterate_before_deadline(response, deadline):
                    if event.get("type") == "done":
                        done_sent = True
                    yield event
        except asyncio.CancelledError:
            # Cooperative cancel must not be converted into an error event.
            raise
        except HTTPException as exc:
            self.logger.warning("WebSocket chat rejected for %s: %s", input_data.user_id, exc.detail)
            yield {"type": "error", "error": exc.detail, "status_code": exc.status_code}
        except asyncio.TimeoutError:
            self.logger.error("WebSocket chat timed out for %s", input_data.user_id)
            yield {"type": "error", "error": "Agent chat timed out.", "status_code": 504}
        except Exception as exc:
            self.logger.error("WebSocket chat event error for %s: %s", input_data.user_id, exc)
            yield {"type": "error", "error": str(exc)}
        finally:
            if acquired:
                self._semaphore.release()
        if not done_sent:
            yield {"type": "done"}

    async def acquire_slot(self) -> None:
        await self._acquire_slot()

    def release_slot(self) -> None:
        self._semaphore.release()

    async def _forward_chat_events(
        self,
        websocket: WebSocket,
        input_data: AgentInput,
        *,
        client: str,
        state: _ChatStreamState,
    ) -> None:
        async with aclosing(self.chat_event_stream(input_data, client=client)) as stream:
            async for event in stream:
                state.observe(event)
                await websocket.send_json(event)

    async def _wait_for_chat_cancel(self, websocket: WebSocket) -> CancelReason:
        try:
            while True:
                raw_payload = await websocket.receive_json()
                if self._is_cancel_payload(raw_payload):
                    return "cancel"
                self.logger.debug(
                    "Ignoring WebSocket payload during active chat turn: %s",
                    type(raw_payload).__name__,
                )
        except WebSocketDisconnect:
            return "disconnect"

    @staticmethod
    def _is_cancel_payload(raw_payload: Any) -> bool:
        if not isinstance(raw_payload, dict):
            return False
        return str(raw_payload.get("type") or "").strip().lower() == "cancel"

    async def _send_cancelled_terminal(self, websocket: WebSocket, state: _ChatStreamState) -> None:
        try:
            if state.open_message_id is not None:
                await websocket.send_json({
                    "type": "message_done",
                    "message_id": state.open_message_id,
                    "phase": "cancelled",
                    "content": "".join(state.content_parts),
                })
                state.open_message_id = None
                state.content_parts = []
            if not state.done_sent:
                await websocket.send_json({"type": "done", "reason": "cancelled"})
                state.done_sent = True
        except (WebSocketDisconnect, RuntimeError):
            # Connection already gone — cancellation still applied server-side.
            self.logger.debug("Could not send cancelled terminal events", exc_info=True)

    @staticmethod
    async def _cancel_task(task: asyncio.Task[Any]) -> None:
        if task.done():
            with suppress(asyncio.CancelledError):
                task.result()
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _acquire_slot(self) -> None:
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self._queue_timeout)
        except asyncio.TimeoutError as exc:
            raise HTTPException(
                status_code=429,
                detail="Too many concurrent chat requests; try again later.",
            ) from exc

    async def _call_agent(self, input_data: ChatInput, *, client: str) -> Any:
        attachments = input_attachments(input_data)
        image_sources = input_image_sources(input_data, attachments=attachments)
        self._record_contact(input_data.user_id)
        context = self._scheduled_delivery_context(input_data, client=client)
        with scheduled_delivery_context(context):
            return await self.agent(
                user_message=input_data.user_message,
                user_id=input_data.user_id,
                image_source=image_sources,
                attachments=attachments,
                channel=CHANNEL_API,
            )

    async def _call_observe(self, input_data: ObserveInput) -> Any:
        return await self.agent.observe(
            context=input_data.context,
            source=input_data.source or "environment",
            event_type=input_data.event_type or "observation",
            metadata=input_data.metadata,
        )

    def _record_contact(self, user_id: str) -> None:
        try:
            upsert_contact(
                self.contacts_file,
                channel=CHANNEL_API,
                user_id=user_id,
                target={"user_id": user_id},
            )
        except Exception:
            self.logger.debug("Failed to record contact for subconscious", exc_info=True)

    @staticmethod
    def _scheduled_delivery_context(input_data: ChatInput, *, client: str) -> ScheduledDeliveryContext:
        return ScheduledDeliveryContext(
            channel=CHANNEL_API,
            user_id=input_data.user_id,
            target={"user_id": input_data.user_id},
            metadata={"source": CHANNEL_API, "client": client},
        )

    async def _await_before_deadline(self, awaitable, deadline: float):
        return await asyncio.wait_for(awaitable, timeout=self._remaining_time(deadline))

    async def _iterate_before_deadline(self, response, deadline: float):
        async with aclosing(response) as stream:
            while True:
                try:
                    yield await asyncio.wait_for(
                        anext(stream),
                        timeout=self._remaining_time(deadline),
                    )
                except StopAsyncIteration:
                    break

    @staticmethod
    def _remaining_time(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        return remaining
