"""Keep a chat socket open so a later message can join the running turn.

The agent decides whether that message is steered or queued. This relay
only delivers that decision while the original stream keeps going. A
``done`` event with ``steered: true`` ends the injected message, not the
turn that was already streaming.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

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


async def relay_chat_socket(websocket: WebSocket, start_stream: Callable[[dict], Any]) -> None:
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
            if await _drive_stream(websocket, stream, incoming, start_stream):
                return
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
    start_stream: Callable[[dict], Any],
) -> bool:
    """Forward one turn. Returns True when the socket has disconnected."""
    events: asyncio.Queue = asyncio.Queue()
    pump = asyncio.create_task(_pump(stream.__aiter__(), events))
    buffered: list[tuple[asyncio.Task, asyncio.Queue]] = []
    disconnected = False
    finished = False
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
                            incoming.put_nowait(item)
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
                    if not await _handle_side_message(websocket, item, start_stream, buffered):
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
            if not await _handle_side_message(websocket, item, start_stream, buffered):
                disconnected = True
                break
        if not disconnected:
            if await _flush_buffered(websocket, buffered):
                buffered = []
            else:
                disconnected = True
        return disconnected
    finally:
        if not event_task.done():
            await _restore_queue_get(event_task, events)
        if not finished and not pump.done():
            pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
        if disconnected:
            for task, _out in buffered:
                task.cancel()
            if buffered:
                await asyncio.gather(*(task for task, _out in buffered), return_exceptions=True)


async def _handle_side_message(
    websocket: WebSocket,
    payload: Any,
    start_stream: Callable[[dict], Any],
    buffered: list[tuple[asyncio.Task, asyncio.Queue]],
) -> bool:
    """Handle a payload that arrived while another turn is streaming.

    Returns False when the socket closed.
    """
    if isinstance(payload, ChatPayloadError):
        return await _send_payload_error(websocket, payload, terminal=False)
    try:
        stream = start_stream(payload)
    except ChatPayloadError as exc:
        return await _send_payload_error(websocket, exc, terminal=False)
    except ValueError as exc:
        return await _send_payload_error(
            websocket,
            ChatPayloadError(str(exc), status_code=422),
            terminal=False,
        )
    source: asyncio.Queue = asyncio.Queue()
    pump = asyncio.create_task(_pump(stream.__aiter__(), source))
    first = await source.get()
    if first is _STREAM_END or isinstance(first, Exception):
        await asyncio.gather(pump, return_exceptions=True)
        if isinstance(first, Exception):
            return await _send_payload_error(
                websocket,
                ChatPayloadError(str(first), status_code=500),
                terminal=False,
            )
        return True
    if isinstance(first, dict) and first.get("type") in {"steered", "error"}:
        if not await _send_json(websocket, first):
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
            return False
        while True:
            nxt = await source.get()
            if nxt is _STREAM_END:
                break
            if isinstance(nxt, Exception):
                await _send_json(websocket, {"type": "error", "error": str(nxt), "status_code": 500})
                break
            if not await _send_json(websocket, nxt):
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)
                return False
        await asyncio.gather(pump, return_exceptions=True)
        return True

    content = ""
    if isinstance(payload, dict):
        content = str(payload.get("user_message") or payload.get("content") or "")
    turn_id = first.get("turn_id") if isinstance(first, dict) else None
    if not await _send_json(websocket, {"type": "queued", "turn_id": turn_id, "content": content}):
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
        return False
    out: asyncio.Queue = asyncio.Queue()
    out.put_nowait(first)
    task = asyncio.create_task(_forward_pumped(source, out, pump))
    buffered.append((task, out))
    return True


async def _forward_pumped(source: asyncio.Queue, out: asyncio.Queue, pump: asyncio.Task) -> None:
    """Move a queued turn's events without advancing that turn on this task."""
    try:
        while True:
            event = await source.get()
            if isinstance(event, Exception):
                await out.put({"type": "error", "error": str(event), "status_code": 500})
                continue
            await out.put(event)
            if event is _STREAM_END:
                return
    except asyncio.CancelledError:
        raise
    finally:
        if not pump.done():
            pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)


async def _flush_buffered(
    websocket: WebSocket,
    buffered: list[tuple[asyncio.Task, asyncio.Queue]],
) -> bool:
    for task, out in buffered:
        while True:
            event = await out.get()
            if event is _STREAM_END:
                break
            if not await _send_json(websocket, event):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                return False
        await task
    return True
