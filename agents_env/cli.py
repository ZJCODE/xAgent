"""CLI for serving and joining an agents environment world."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Optional

from . import DEFAULT_HOST, DEFAULT_PORT, __version__
from .scene import DEFAULT_PLAZA_SCENE, parse_scene_config


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


async def _cmd_serve(args: argparse.Namespace) -> int:
    import logging

    import yaml

    from .server import WorldServer

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    data_root = args.data_root or None
    if args.scene:
        scene_path = Path(args.scene).expanduser().resolve()
        server = WorldServer.from_scene_path(
            str(scene_path),
            host=args.host,
            port=args.port,
            data_root=data_root,
        )
    else:
        scene = parse_scene_config(yaml.safe_load(DEFAULT_PLAZA_SCENE))
        server = WorldServer.from_scene(
            scene,
            host=args.host,
            port=args.port,
            data_root=data_root,
        )
    try:
        await server.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 0
    return 0


async def _cmd_join(args: argparse.Namespace) -> int:
    from .client import WorldClient

    member_id = args.member_id
    name = args.name or member_id
    room_id = args.room
    url = args.url

    print(f"connecting to {url} as {name}({member_id}) …", flush=True)
    async with WorldClient(url, member_id=member_id, display_name=name) as client:
        welcome = client.welcome or {}
        if welcome.get("type") != "welcome":
            print(f"unexpected: {welcome}", file=sys.stderr)
            return 1
        print(
            f"world={welcome.get('world_id')} "
            f"protocol={welcome.get('protocol_version')} "
            f"rooms={[r.get('id') for r in welcome.get('rooms', [])]}"
        )
        await client.join(room_id)
        print(
            f"joined {room_id}. Type messages and Enter. /leave to leave, /quit to exit.",
            flush=True,
        )

        stop = asyncio.Event()

        async def printer() -> None:
            try:
                async for msg in client.events():
                    _print_server_message(msg)
            except Exception:
                pass
            finally:
                stop.set()

        printer_task = asyncio.create_task(printer())
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
                    await client.leave(room_id)
                    continue
                await client.speak(room_id, text)
        finally:
            stop.set()
            printer_task.cancel()
            try:
                await printer_task
            except asyncio.CancelledError:
                pass
    return 0


async def _cmd_dummy(args: argparse.Namespace) -> int:
    from .client import WorldClient

    member_id = args.member_id
    name = args.name or member_id
    room_id = args.room
    url = args.url

    async with WorldClient(url, member_id=member_id, display_name=name) as client:
        print(json.dumps(client.welcome, ensure_ascii=False), flush=True)
        await client.join(room_id)
        while True:
            msg = await client.recv(timeout=5.0)
            print(json.dumps(msg, ensure_ascii=False), flush=True)
            if msg.get("type") == "snapshot":
                break

        for line in args.lines:
            await asyncio.sleep(args.delay)
            await client.speak(room_id, line)

        listen_until = asyncio.get_running_loop().time() + max(0.0, float(args.listen))
        while True:
            remaining = listen_until - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                msg = await client.recv(timeout=remaining)
            except asyncio.TimeoutError:
                break
            print(json.dumps(msg, ensure_ascii=False), flush=True)

        if args.leave:
            await client.leave(room_id)
            try:
                msg = await client.recv(timeout=1.0)
                print(json.dumps(msg, ensure_ascii=False), flush=True)
            except asyncio.TimeoutError:
                pass
    return 0


def _print_server_message(msg: dict[str, Any]) -> None:
    msg_type = msg.get("type")
    if msg_type == "event":
        kind = msg.get("kind")
        actor = msg.get("actor_id")
        text = msg.get("text")
        seq = msg.get("seq")
        room_seq = msg.get("room_seq")
        if kind in {"join", "leave"} and not text:
            print(f"[{seq}/{room_seq}] {kind} {actor}", flush=True)
            return
        print(f"[{seq}/{room_seq}] {kind} {actor}: {text}", flush=True)
        return
    if msg_type == "snapshot":
        if msg.get("sync"):
            extra = ""
            if msg.get("has_more"):
                extra = f", has_more next={msg.get('next_after_seq')}"
            print(
                f"(sync {msg.get('room_id')} after {msg.get('after_seq')}: "
                f"{len(msg.get('events') or [])} events{extra})",
                flush=True,
            )
            return
        print(f"--- room {msg.get('room_id')} / {msg.get('name')} ---", flush=True)
        if msg.get("setting"):
            print(f"setting: {msg.get('setting')}", flush=True)
        present = msg.get("present") or []
        names = [f"{p.get('display_name')}({p.get('member_id')})" for p in present]
        print(f"present: {', '.join(names) if names else '(empty)'}", flush=True)
        for event in msg.get("events") or []:
            text = event.get("text")
            kind = event.get("kind")
            suffix = f": {text}" if text else ""
            print(
                f"  [{event.get('seq')}/{event.get('room_seq')}] "
                f"{kind} {event.get('actor_id')}{suffix}",
                flush=True,
            )
        print("---", flush=True)
        return
    if msg_type == "error":
        code = msg.get("code")
        prefix = f"{code}: " if code else ""
        print(f"! error: {prefix}{msg.get('message')}", file=sys.stderr, flush=True)
        return
    if msg_type == "lagged":
        print(
            f"! lagged in {msg.get('room_id')}; sync after {msg.get('after_seq')}",
            file=sys.stderr,
            flush=True,
        )
        return
    print(json.dumps(msg, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
