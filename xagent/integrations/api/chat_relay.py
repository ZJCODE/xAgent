"""Keep a chat socket readable while a turn is streaming.

A later payload is offered to ``steer()`` with the running turn id. Success
sends ``steered`` and a ``done`` that ends only that message. Failure puts
the original payload back, and it is handled as a normal chat after this
stream finishes. The relay does not start a second turn to find out.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

_STREAM_END = object()


class ChatPayloadError(Exception):
    """A chat frame that should be reported without dropping the socket."""

    def __init__(self, message: str, *, status_code: int = 422, details: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.details = details


def _error_event(error: ChatPayloadError) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "error",
        "error": str(error),
        "status_code": error.status_code,
    }
    if error.details is not None:
        event["details"] = error.details
    return event


async def _restore_queue_get(task: asyncio.Task, queue: asyncio.Queue) -> None:
    """Cancel a pending get without losing an item it already took."""
    if task.done():
        if not task.cancelled() and task.exception() is None:
            queue.put_nowait(task.result())
        return
    task.cancel()
    try:
        item = await task
    except asyncio.CancelledError:
        return
    except Exception:
        logger.debug("Chat socket read failed while cancelling", exc_info=True)
        return
    queue.put_nowait(item)


async def _send_json(websocket: WebSocket, payload: dict) -> bool:
    try:
        await websocket.send_json(payload)
    except WebSocketDisconnect:
        return False
    return True


async def _send_payload_error(websocket: WebSocket, error: ChatPayloadError, *, terminal: bool) -> bool:
    if not await _send_json(websocket, _error_event(error)):
        return False
    if terminal and not await _send_json(websocket, {"type": "done"}):
        return False
    return True


async def relay_chat_socket(
    websocket: WebSocket,
    start_stream: Callable[[dict], Any],
    steer_payload: Callable[[str, dict], Awaitable[bool]],
) -> None:
    """Read chat payloads until the socket closes, including during a stream."""
    incoming: asyncio.Queue = asyncio.Queue()

    async def reader() -> None:
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    await incoming.put(None)
                    return
                if message["type"] != "websocket.receive":
                    continue
                raw = message.get("text")
                if raw is None and message.get("bytes") is not None:
                    raw = bytes(message["bytes"]).decode()
                if raw is None:
                    continue
                try:
                    await incoming.put(json.loads(raw))
                except json.JSONDecodeError as exc:
                    await incoming.put(ChatPayloadError(
                        "Invalid JSON payload.",
                        status_code=400,
                        details=str(exc),
                    ))
        except WebSocketDisconnect:
            await incoming.put(None)
        except Exception:
            logger.debug("Chat socket reader stopped", exc_info=True)
            await incoming.put(None)

    reader_task = asyncio.create_task(reader())
    try:
        while True:
            payload = await incoming.get()
            if payload is None:
                return
            if isinstance(payload, ChatPayloadError):
                if not await _send_payload_error(websocket, payload, terminal=True):
                    return
                continue
            try:
                stream = start_stream(payload)
            except ChatPayloadError as exc:
                if not await _send_payload_error(websocket, exc, terminal=True):
                    return
                continue
            except ValueError as exc:
                if not await _send_payload_error(
                    websocket,
                    ChatPayloadError(str(exc), status_code=422),
                    terminal=True,
                ):
                    return
                continue
            deferred: list[Any] = []
            if await _drive_stream(websocket, stream, incoming, steer_payload, deferred):
                return
            for item in deferred:
                incoming.put_nowait(item)
    finally:
        reader_task.cancel()
        await asyncio.gather(reader_task, return_exceptions=True)


async def _pump(iterator: Any, out: asyncio.Queue) -> None:
    """Advance one chat stream on a single task so its context stays valid."""
    try:
        while True:
            try:
                event = await iterator.__anext__()
            except StopAsyncIteration:
                await out.put(_STREAM_END)
                return
            await out.put(event)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Chat stream failed: %s", exc)
        await out.put(exc)
        await out.put(_STREAM_END)
    finally:
        close = getattr(iterator, "aclose", None)
        if callable(close):
            await close()


async def _drive_stream(
    websocket: WebSocket,
    stream: Any,
    incoming: asyncio.Queue,
    steer_payload: Callable[[str, dict], Awaitable[bool]],
    deferred: list[Any],
) -> bool:
    """Forward one turn. Returns True when the socket has disconnected."""
    events: asyncio.Queue = asyncio.Queue()
    pump = asyncio.create_task(_pump(stream.__aiter__(), events))
    turn_id: str | None = None
    finished = False
    disconnected = False
    event_task = asyncio.create_task(events.get())
    try:
        while True:
            incoming_task = asyncio.create_task(incoming.get())
            done, _pending = await asyncio.wait(
                {event_task, incoming_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if event_task in done:
                event = event_task.result()
                if event is _STREAM_END or isinstance(event, Exception):
                    finished = True
                    if incoming_task in done:
                        item = incoming_task.result()
                        if item is None:
                            disconnected = True
                        else:
                            deferred.append(item)
                    else:
                        await _restore_queue_get(incoming_task, incoming)
                    if isinstance(event, Exception):
                        await _send_json(websocket, {
                            "type": "error",
                            "error": str(event),
                            "status_code": 500,
                        })
                        await _send_json(websocket, {"type": "done"})
                    break
                if isinstance(event, dict) and event.get("type") == "accepted" and event.get("turn_id"):
                    turn_id = str(event["turn_id"])
                if not await _send_json(websocket, event):
                    disconnected = True
                    if incoming_task not in done:
                        await _restore_queue_get(incoming_task, incoming)
                    else:
                        incoming.put_nowait(incoming_task.result())
                    break
                if incoming_task in done:
                    item = incoming_task.result()
                    if item is None:
                        disconnected = True
                        break
                    if not await _offer_steer(websocket, item, turn_id, steer_payload, deferred):
                        disconnected = True
                        break
                else:
                    await _restore_queue_get(incoming_task, incoming)
                event_task = asyncio.create_task(events.get())
                continue

            item = incoming_task.result()
            if item is None:
                disconnected = True
                break
            if not await _offer_steer(websocket, item, turn_id, steer_payload, deferred):
                disconnected = True
                break
        return disconnected
    finally:
        if not event_task.done():
            await _restore_queue_get(event_task, events)
        if not finished and not pump.done():
            pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)


async def _offer_steer(
    websocket: WebSocket,
    payload: Any,
    turn_id: str | None,
    steer_payload: Callable[[str, dict], Awaitable[bool]],
    deferred: list[Any],
) -> bool:
    """Try to join the running turn. Returns False when the socket closed."""
    if isinstance(payload, ChatPayloadError):
        return await _send_payload_error(websocket, payload, terminal=False)
    if not turn_id:
        deferred.append(payload)
        return True
    try:
        steered = await steer_payload(turn_id, payload)
    except ChatPayloadError as exc:
        return await _send_payload_error(websocket, exc, terminal=False)
    except ValueError as exc:
        return await _send_payload_error(
            websocket,
            ChatPayloadError(str(exc), status_code=422),
            terminal=False,
        )
    if not steered:
        deferred.append(payload)
        return True
    content = ""
    user_id = None
    if isinstance(payload, dict):
        content = str(payload.get("user_message") or payload.get("content") or "")
        user_id = payload.get("user_id")
    if not await _send_json(websocket, {
        "type": "steered",
        "turn_id": turn_id,
        "content": content,
        "user_id": user_id,
        "channel": "api",
    }):
        return False
    return await _send_json(websocket, {"type": "done", "turn_id": turn_id, "steered": True})
