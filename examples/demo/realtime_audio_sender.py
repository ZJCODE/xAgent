"""Send audio chunks to the xAgent realtime gateway and print resulting events.

The gateway forwards raw chunk payloads to the configured realtime provider.
The input file should already be in a provider-compatible audio format.
For OpenAI Realtime this usually means PCM16 mono audio prepared by the caller.

Usage:
    pip install websockets
    python examples/demo/realtime_audio_sender.py --file ./sample_audio.raw
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import pathlib
import uuid
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener
from urllib.parse import urlsplit

try:
    import websockets
except ModuleNotFoundError as exc:  # pragma: no cover - import guard for demo use
    raise SystemExit(
        "This demo requires the `websockets` package. Install it with `pip install websockets`."
    ) from exc


async def send_json(websocket, payload: dict) -> None:
    await websocket.send(json.dumps(payload))


async def wait_for_session(websocket) -> tuple[str, str]:
    while True:
        raw = await websocket.recv()
        event = json.loads(raw)
        print(event)
        if event.get("type") == "session.state":
            return event["realtime_session_id"], event["conversation_id"]


async def drain_events(websocket, timeout_seconds: float) -> None:
    while True:
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            print({"type": "timeout", "message": "No more events within timeout window."})
            return

        event = json.loads(raw)
        print(event)
        if event.get("type") in {"turn.completed", "job.completed", "job.failed"}:
            return


async def main() -> None:
    parser = argparse.ArgumentParser(description="Send audio chunks to the xAgent realtime gateway")
    parser.add_argument(
        "--ws-url",
        default="ws://127.0.0.1:8010/realtime/ws",
        help="Realtime websocket URL",
    )
    parser.add_argument(
        "--user-id",
        default=f"audio_demo_{uuid.uuid4().hex[:6]}",
        help="User ID for the realtime session",
    )
    parser.add_argument(
        "--conversation-id",
        default=None,
        help="Existing conversation ID to resume",
    )
    parser.add_argument(
        "--file",
        required=True,
        help="Path to the provider-ready audio payload to send",
    )
    parser.add_argument(
        "--chunk-bytes",
        type=int,
        default=4096,
        help="Number of raw bytes to base64-encode per websocket event",
    )
    parser.add_argument(
        "--event-timeout",
        type=float,
        default=10.0,
        help="How long to wait for follow-up events after commit",
    )
    args = parser.parse_args()

    audio_path = pathlib.Path(args.file)
    if not audio_path.exists():
        raise SystemExit(f"Audio file not found: {audio_path}")

    raw_audio = audio_path.read_bytes()
    if not raw_audio:
        raise SystemExit(f"Audio file is empty: {audio_path}")

    try:
        connect_kwargs = {}
        host = urlsplit(args.ws_url).hostname or ""
        if host in {"127.0.0.1", "localhost", "::1"}:
            connect_kwargs["proxy"] = None

        async with websockets.connect(args.ws_url, **connect_kwargs) as websocket:
            await send_json(
                websocket,
                {
                    "type": "session.start",
                    "user_id": args.user_id,
                    "conversation_id": args.conversation_id,
                },
            )
            realtime_session_id, conversation_id = await wait_for_session(websocket)

            for offset in range(0, len(raw_audio), args.chunk_bytes):
                chunk = raw_audio[offset : offset + args.chunk_bytes]
                encoded = base64.b64encode(chunk).decode("ascii")
                await send_json(
                    websocket,
                    {
                        "type": "input.audio.chunk",
                        "realtime_session_id": realtime_session_id,
                        "conversation_id": conversation_id,
                        "payload": {"audio": encoded},
                    },
                )

            await send_json(
                websocket,
                {
                    "type": "turn.commit",
                    "realtime_session_id": realtime_session_id,
                    "conversation_id": conversation_id,
                    "turn_id": "turn_audio_1",
                    "payload": {},
                },
            )
            await drain_events(websocket, timeout_seconds=args.event_timeout)
    except Exception as exc:
        parsed = urlsplit(args.ws_url)
        scheme = "https" if parsed.scheme == "wss" else "http"
        health_url = f"{scheme}://{parsed.netloc}/health"
        print(f"Failed to connect to realtime websocket: {exc}")
        print(f"Expected realtime websocket: {args.ws_url}")
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
        print("Suggested next step:")
        print("  python examples/demo/realtime_showcase_server.py --port 8011")
        print("  python examples/demo/realtime_audio_sender.py --ws-url ws://127.0.0.1:8011/realtime/ws --file ./sample_audio.raw")


if __name__ == "__main__":
    asyncio.run(main())
