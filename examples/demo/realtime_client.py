"""Interactive client for the xAgent realtime gateway.

Usage:
    pip install websockets
    python examples/demo/realtime_client.py --ws-url ws://127.0.0.1:8010/realtime/ws
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from dataclasses import dataclass
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener
from typing import Optional
from urllib.parse import urlsplit

try:
    import websockets
except ModuleNotFoundError as exc:  # pragma: no cover - import guard for demo use
    raise SystemExit(
        "This demo requires the `websockets` package. Install it with `pip install websockets`."
    ) from exc


@dataclass
class SessionContext:
    user_id: str
    conversation_id: Optional[str] = None
    realtime_session_id: Optional[str] = None
    turn_counter: int = 0

    def next_turn_id(self) -> str:
        self.turn_counter += 1
        return f"turn_{self.turn_counter}"


class RealtimeConsoleClient:
    def __init__(
        self,
        ws_url: str,
        user_id: str,
        conversation_id: Optional[str] = None,
        enable_memory: bool = False,
        history_count: int = 16,
        max_iter: int = 10,
        max_concurrent_tools: int = 10,
    ):
        self.ws_url = ws_url
        self.context = SessionContext(
            user_id=user_id,
            conversation_id=conversation_id,
        )
        self.enable_memory = enable_memory
        self.history_count = history_count
        self.max_iter = max_iter
        self.max_concurrent_tools = max_concurrent_tools
        self.websocket = None
        self._stream_open = False
        self._stop = asyncio.Event()

    async def run(self) -> None:
        try:
            connect_kwargs = self._build_connect_kwargs()
            async with websockets.connect(self.ws_url, **connect_kwargs) as websocket:
                self.websocket = websocket
                receiver = asyncio.create_task(self._receive_loop())
                await self._start_session()
                await self._input_loop()
                self._stop.set()
                receiver.cancel()
                try:
                    await receiver
                except asyncio.CancelledError:
                    pass
        except Exception as exc:
            await self._report_connection_failure(exc)

    async def _report_connection_failure(self, exc: Exception) -> None:
        print(f"Failed to connect to realtime websocket: {exc}")
        health_url = self._health_url()
        print(f"Expected realtime websocket: {self.ws_url}")
        print(f"Expected health endpoint: {health_url}")
        try:
            opener = build_opener(ProxyHandler({}))
            response = await asyncio.to_thread(opener.open, health_url, timeout=2)
            try:
                status = getattr(response, "status", "unknown")
                print(f"Health check responded with HTTP {status}.")
            finally:
                response.close()
        except URLError as health_exc:
            print(f"Health check failed: {health_exc}")

        print("Common causes:")
        print("  1. The showcase server is not running.")
        print("  2. A global proxy intercepted the local websocket connection.")
        print("  3. The websocket URL points to the wrong host or port.")
        print("Suggested next step:")
        print("  python examples/demo/realtime_showcase_server.py --port 8011")
        print("  python examples/demo/realtime_client.py --ws-url ws://127.0.0.1:8011/realtime/ws")

    def _build_connect_kwargs(self) -> dict:
        kwargs = {}
        host = urlsplit(self.ws_url).hostname or ""
        if host in {"127.0.0.1", "localhost", "::1"}:
            kwargs["proxy"] = None
        return kwargs

    def _health_url(self) -> str:
        parsed = urlsplit(self.ws_url)
        scheme = "https" if parsed.scheme == "wss" else "http"
        return f"{scheme}://{parsed.netloc}/health"

    async def _start_session(self) -> None:
        await self._send_event(
            {
                "type": "session.start",
                "user_id": self.context.user_id,
                "conversation_id": self.context.conversation_id,
            }
        )

    async def _input_loop(self) -> None:
        self._print_help()
        while not self._stop.is_set():
            line = await asyncio.to_thread(input, "\nrealtime> ")
            line = line.strip()
            if not line:
                continue

            if line == "/quit":
                await self._close_session()
                return

            if line == "/help":
                self._print_help()
                continue

            if line == "/interrupt":
                await self._send_control("interrupt")
                continue

            if line == "/commit":
                await self._commit_turn({})
                continue

            if line.startswith("/audio "):
                payload = line[len("/audio ") :].strip()
                await self._send_control("input.audio.chunk", {"audio": payload})
                continue

            if line.startswith("/tool "):
                parts = line.split(" ", 2)
                if len(parts) < 3:
                    print("Usage: /tool <tool_name> <text>")
                    continue
                tool_name, text = parts[1], parts[2]
                await self._send_control("input.text", {"text": text})
                await self._commit_turn({"tool_name": tool_name})
                continue

            await self._send_control("input.text", {"text": line})
            await self._commit_turn({})

    async def _commit_turn(self, payload: dict) -> None:
        turn_payload = {
            "enable_memory": self.enable_memory,
            "history_count": self.history_count,
            "max_iter": self.max_iter,
            "max_concurrent_tools": self.max_concurrent_tools,
        }
        turn_payload.update(payload)
        await self._send_control(
            "turn.commit",
            turn_payload,
            turn_id=self.context.next_turn_id(),
        )

    async def _close_session(self) -> None:
        if self.context.realtime_session_id is None:
            return
        await self._send_control("session.close")

    async def _send_control(
        self,
        event_type: str,
        payload: Optional[dict] = None,
        turn_id: Optional[str] = None,
    ) -> None:
        if self.context.realtime_session_id is None and event_type != "session.start":
            print("Session is not ready yet. Wait for `session.state` first.")
            return
        event = {
            "type": event_type,
            "realtime_session_id": self.context.realtime_session_id,
            "conversation_id": self.context.conversation_id,
            "turn_id": turn_id,
            "payload": payload or {},
        }
        await self._send_event(event)

    async def _send_event(self, event: dict) -> None:
        if self.websocket is None:
            raise RuntimeError("WebSocket connection is not established.")
        await self.websocket.send(json.dumps(event))

    async def _receive_loop(self) -> None:
        assert self.websocket is not None
        async for raw_message in self.websocket:
            message = json.loads(raw_message)
            event_type = message.get("type")
            payload = message.get("payload", {})

            if event_type == "session.state":
                if message.get("conversation_id"):
                    self.context.conversation_id = message["conversation_id"]
                if message.get("realtime_session_id"):
                    self.context.realtime_session_id = message["realtime_session_id"]
                print(
                    "\n[session]",
                    f"conversation_id={self.context.conversation_id}",
                    f"realtime_session_id={self.context.realtime_session_id}",
                    f"status={payload.get('status')}",
                )
                continue

            if event_type == "ack":
                received = payload.get("received")
                if received:
                    print(f"\n[ack] {received}")
                continue

            if event_type == "partial_text":
                delta = payload.get("delta", "")
                if delta:
                    if not self._stream_open:
                        print("\n[assistant] ", end="", flush=True)
                        self._stream_open = True
                    print(delta, end="", flush=True)
                continue

            if event_type == "turn.started":
                print(f"\n[turn.started] {message.get('turn_id')}")
                continue

            if event_type == "turn.completed":
                if self._stream_open:
                    print()
                    self._stream_open = False
                if "error" in payload:
                    print(f"[turn.completed] error={payload['error']}")
                else:
                    print(
                        f"[turn.completed] response_id={payload.get('response_id')} "
                        f"output={payload.get('output_text')}"
                    )
                continue

            if event_type in {"job.started", "job.progress", "job.completed", "job.failed"}:
                print(f"\n[{event_type}] {payload}")
                continue

            print(f"\n[event] {message}")

    def _print_help(self) -> None:
        print("Commands:")
        print("  <text>                 Send a text turn and auto-commit it")
        print("  /tool NAME TEXT        Run a committed turn with a specific tool hint")
        print("  /audio BASE64          Append one audio chunk to the current turn buffer")
        print("  /commit                Commit the current buffered turn")
        print("  /interrupt             Cancel the active turn or background job")
        print("  /help                  Show this help")
        print("  /quit                  Close the realtime session and exit")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Interactive client for xAgent realtime gateway")
    parser.add_argument(
        "--ws-url",
        default="ws://127.0.0.1:8010/realtime/ws",
        help="Realtime websocket URL",
    )
    parser.add_argument(
        "--user-id",
        default=f"demo_user_{uuid.uuid4().hex[:6]}",
        help="User ID for the realtime session",
    )
    parser.add_argument(
        "--conversation-id",
        default=None,
        help="Existing conversation ID to resume",
    )
    parser.add_argument(
        "--enable-memory",
        action="store_true",
        help="Enable memory when committing text turns",
    )
    parser.add_argument("--history-count", type=int, default=16, help="History window for Responses turns")
    parser.add_argument("--max-iter", type=int, default=10, help="Maximum Responses iterations")
    parser.add_argument(
        "--max-concurrent-tools",
        type=int,
        default=10,
        help="Maximum concurrent tool calls for Responses turns",
    )
    return parser


async def main() -> None:
    args = build_parser().parse_args()
    client = RealtimeConsoleClient(
        ws_url=args.ws_url,
        user_id=args.user_id,
        conversation_id=args.conversation_id,
        enable_memory=args.enable_memory,
        history_count=args.history_count,
        max_iter=args.max_iter,
        max_concurrent_tools=args.max_concurrent_tools,
    )
    await client.run()


if __name__ == "__main__":
    asyncio.run(main())
