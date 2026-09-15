"""CLI for serving and joining an agents environment world."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Optional

from . import DEFAULT_HOST, DEFAULT_PORT, __version__
from .scene import DEFAULT_PLAZA_SCENE


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agents-env",
        description="Independent agents environment: a world process, not an agent runtime.",
    )
    parser.add_argument("--version", action="version", version=f"agents-env {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve_p = sub.add_parser("serve", help="Start the world WebSocket process")
    serve_p.add_argument(
        "--scene",
        type=str,
        default="",
        help="Path to scene YAML (default: built-in plaza)",
    )
    serve_p.add_argument("--host", default=DEFAULT_HOST)
    serve_p.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve_p.add_argument(
        "--data-root",
        default="",
        help="Override data root (default: ~/.agents-env)",
    )

    join_p = sub.add_parser("join", help="Human client: join a room and chat")
    join_p.add_argument("--url", default=f"ws://{DEFAULT_HOST}:{DEFAULT_PORT}")
    join_p.add_argument("--member-id", required=True)
    join_p.add_argument("--name", default="", help="Display name (default: member-id)")
    join_p.add_argument("--room", default="hall")

    dummy_p = sub.add_parser("dummy", help="Scripted client for venue proofs")
    dummy_p.add_argument("--url", default=f"ws://{DEFAULT_HOST}:{DEFAULT_PORT}")
    dummy_p.add_argument("--member-id", default="dummy")
    dummy_p.add_argument("--name", default="Dummy")
    dummy_p.add_argument("--room", default="hall")
    dummy_p.add_argument(
        "--lines",
        nargs="*",
        default=["hello from dummy", "anyone here?"],
        help="Utterances to speak after joining",
    )
    dummy_p.add_argument("--delay", type=float, default=0.5, help="Delay between lines")
    dummy_p.add_argument(
        "--listen",
        type=float,
        default=2.0,
        help="Seconds to keep listening after speaking",
    )
    dummy_p.add_argument(
        "--leave",
        action="store_true",
        help="Leave the room before disconnecting",
    )

    args = parser.parse_args(argv)
    if args.command == "serve":
        return asyncio.run(_cmd_serve(args))
    if args.command == "join":
        return asyncio.run(_cmd_join(args))
    if args.command == "dummy":
        return asyncio.run(_cmd_dummy(args))
    parser.error(f"unknown command: {args.command}")
    return 2


def _resolve_scene_path(scene_arg: str) -> Path:
    if scene_arg:
        return Path(scene_arg).expanduser().resolve()
    examples = Path(__file__).resolve().parent / "examples" / "plaza.yaml"
    if examples.is_file():
        return examples
    # Fallback: write built-in into a temp-less path under package examples expectation.
    examples.parent.mkdir(parents=True, exist_ok=True)
    examples.write_text(DEFAULT_PLAZA_SCENE, encoding="utf-8")
    return examples


async def _cmd_serve(args: argparse.Namespace) -> int:
    import logging

    from .server import WorldServer

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    scene_path = _resolve_scene_path(args.scene)
    data_root = args.data_root or None
    server = WorldServer.from_scene_path(
        str(scene_path),
        host=args.host,
        port=args.port,
        data_root=data_root,
    )
    try:
        await server.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 0
    finally:
        await server.world.close()
    return 0


async def _cmd_join(args: argparse.Namespace) -> int:
    from websockets.asyncio.client import connect

    member_id = args.member_id
    name = args.name or member_id
    room_id = args.room
    url = args.url

    print(f"connecting to {url} as {name}({member_id}) …", flush=True)
    async with connect(url) as ws:
        await ws.send(
            json.dumps(
                {"type": "hello", "member_id": member_id, "display_name": name},
                ensure_ascii=False,
            )
        )
        welcome = json.loads(await ws.recv())
        if welcome.get("type") != "welcome":
            print(f"unexpected: {welcome}", file=sys.stderr)
            return 1
        print(f"world={welcome.get('world_id')} rooms={[r.get('id') for r in welcome.get('rooms', [])]}")

        await ws.send(json.dumps({"type": "join", "room_id": room_id}, ensure_ascii=False))
        print(f"joined {room_id}. Type messages and Enter. /leave to leave, /quit to exit.", flush=True)

        stop = asyncio.Event()

        async def reader() -> None:
            try:
                async for raw in ws:
                    _print_server_message(json.loads(raw))
            except Exception:
                pass
            finally:
                stop.set()

        reader_task = asyncio.create_task(reader())
        loop = asyncio.get_running_loop()

        try:
            while not stop.is_set():
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if line == "":
                    break
                text = line.rstrip("\n")
                if not text:
                    continue
                if text in {"/quit", "/exit"}:
                    break
                if text == "/leave":
                    await ws.send(json.dumps({"type": "leave", "room_id": room_id}))
                    continue
                await ws.send(
                    json.dumps(
                        {"type": "speak", "room_id": room_id, "text": text},
                        ensure_ascii=False,
                    )
                )
        finally:
            stop.set()
            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass
    return 0


async def _cmd_dummy(args: argparse.Namespace) -> int:
    from websockets.asyncio.client import connect

    member_id = args.member_id
    name = args.name or member_id
    room_id = args.room
    url = args.url

    async with connect(url) as ws:
        await ws.send(
            json.dumps(
                {"type": "hello", "member_id": member_id, "display_name": name},
                ensure_ascii=False,
            )
        )
        welcome = json.loads(await ws.recv())
        if welcome.get("type") != "welcome":
            print(json.dumps(welcome, ensure_ascii=False))
            return 1
        print(json.dumps(welcome, ensure_ascii=False), flush=True)

        await ws.send(json.dumps({"type": "join", "room_id": room_id}))
        inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def reader() -> None:
            async for raw in ws:
                msg = json.loads(raw)
                await inbox.put(msg)
                print(json.dumps(msg, ensure_ascii=False), flush=True)

        reader_task = asyncio.create_task(reader())
        # Wait for snapshot
        while True:
            msg = await inbox.get()
            if msg.get("type") == "snapshot":
                break

        for line in args.lines:
            await asyncio.sleep(args.delay)
            await ws.send(
                json.dumps(
                    {"type": "speak", "room_id": room_id, "text": line},
                    ensure_ascii=False,
                )
            )

        await asyncio.sleep(max(0.0, float(args.listen)))
        if args.leave:
            await ws.send(json.dumps({"type": "leave", "room_id": room_id}))
            await asyncio.sleep(0.2)

        reader_task.cancel()
        try:
            await reader_task
        except asyncio.CancelledError:
            pass
    return 0


def _print_server_message(msg: dict[str, Any]) -> None:
    msg_type = msg.get("type")
    if msg_type == "event":
        kind = msg.get("kind")
        actor = msg.get("actor_id")
        text = msg.get("text")
        seq = msg.get("seq")
        print(f"[{seq}] {kind} {actor}: {text}", flush=True)
        return
    if msg_type == "snapshot":
        if msg.get("sync"):
            print(f"(sync {msg.get('room_id')} after {msg.get('after_seq')}: {len(msg.get('events') or [])} events)", flush=True)
            return
        print(f"--- room {msg.get('room_id')} / {msg.get('name')} ---", flush=True)
        if msg.get("setting"):
            print(f"setting: {msg.get('setting')}", flush=True)
        present = msg.get("present") or []
        names = [f"{p.get('display_name')}({p.get('member_id')})" for p in present]
        print(f"present: {', '.join(names) if names else '(empty)'}", flush=True)
        for event in msg.get("events") or []:
            print(f"  [{event.get('seq')}] {event.get('kind')} {event.get('actor_id')}: {event.get('text')}", flush=True)
        print("---", flush=True)
        return
    if msg_type == "error":
        print(f"! error: {msg.get('message')}", file=sys.stderr, flush=True)
        return
    print(json.dumps(msg, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
