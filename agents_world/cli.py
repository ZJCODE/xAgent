"""CLI for the world hub: serve, create, join, dummy."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Optional

from urllib.parse import quote

from dataclasses import dataclass

from . import DEFAULT_HOST, DEFAULT_PORT, __version__
from .chat_format import format_chat_event, format_join_intro
from .config import WorldConfig
from .paths import allocate_world_id, list_world_ids, remove_world_dir, world_data_dir
from .store import open_store_for_world


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agents-world",
        description="Independent world hub, not an agent runtime.",
    )
    parser.add_argument("--version", action="version", version=f"agents-world {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve_p = sub.add_parser("serve", help="Start the world hub")
    serve_p.add_argument("--host", default=DEFAULT_HOST)
    serve_p.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve_p.add_argument(
        "--data-root",
        default="",
        help="Override data root (default: ~/.xagent)",
    )

    create_p = sub.add_parser("create", help="Create a world on disk (no server)")
    create_p.add_argument("--name", required=True, help="World display name")
    create_p.add_argument(
        "--data-root",
        default="",
        help="Override data root (default: ~/.xagent)",
    )

    remove_p = sub.add_parser("remove", help="Delete a world on disk (no server)")
    remove_p.add_argument("--world-id", required=True, help="World to delete")
    remove_p.add_argument(
        "--data-root",
        default="",
        help="Override data root (default: ~/.xagent)",
    )

    join_p = sub.add_parser("join", help="Human client: join a world and chat")
    join_p.add_argument("--world-id", required=True, help="World to join")
    join_p.add_argument("--host", default=DEFAULT_HOST)
    join_p.add_argument("--port", type=int, default=DEFAULT_PORT)
    join_p.add_argument("--url", default="", help="Full ws URL (overrides host/port/world-id)")
    join_p.add_argument("--member-id", required=True)
    join_p.add_argument("--name", default="", help="Display name (default: member-id)")
    join_p.add_argument(
        "--full-history",
        action="store_true",
        help="Print the full event log when joining (default: summary only)",
    )
    join_p.add_argument(
        "--raw",
        action="store_true",
        help="Print protocol-style lines ([seq] kind actor) instead of chat formatting",
    )

    dummy_p = sub.add_parser("dummy", help="Scripted client for venue proofs")
    dummy_p.add_argument("--world-id", required=True, help="World to join")
    dummy_p.add_argument("--host", default=DEFAULT_HOST)
    dummy_p.add_argument("--port", type=int, default=DEFAULT_PORT)
    dummy_p.add_argument("--url", default="", help="Full ws URL (overrides host/port/world-id)")
    dummy_p.add_argument("--member-id", default="dummy")
    dummy_p.add_argument("--name", default="Dummy")
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
        help="Leave the world before disconnecting",
    )

    args = parser.parse_args(argv)
    if args.command == "serve":
        return asyncio.run(_cmd_serve(args))
    if args.command == "create":
        return _cmd_create(args)
    if args.command == "remove":
        return _cmd_remove(args)
    if args.command == "join":
        return asyncio.run(_cmd_join(args))
    if args.command == "dummy":
        return asyncio.run(_cmd_dummy(args))
    parser.error(f"unknown command: {args.command}")
    return 2


def _ws_url(args: argparse.Namespace) -> str:
    if args.url:
        return str(args.url).strip()
    return f"ws://{args.host}:{args.port}/ws/{quote(str(args.world_id), safe='')}"


def _cmd_create(args: argparse.Namespace) -> int:
    root = args.data_root or None
    name = str(args.name or "").strip()
    if not name:
        print("name is required", file=sys.stderr)
        return 1
    world_id = allocate_world_id(name, taken=list_world_ids(root=root))
    config = WorldConfig.create(world_id=world_id, name=name)
    data_dir = world_data_dir(config.world_id, root=root)
    if (data_dir / "world.sqlite3").is_file():
        print(f"world already exists: {config.world_id}", file=sys.stderr)
        return 1
    store = open_store_for_world(config, root=root or None)
    store.close()
    print(json.dumps({"id": config.world_id, "name": config.name}))
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    root = args.data_root or None
    world_id = str(args.world_id or "").strip()
    if not world_id:
        print("world_id is required", file=sys.stderr)
        return 1
    try:
        removed = remove_world_dir(world_id, root=root)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"id": world_id, "deleted": True, "path": str(removed)}))
    return 0


async def _cmd_serve(args: argparse.Namespace) -> int:
    import logging

    from .server import WorldHub

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    hub = WorldHub.create(
        host=args.host,
        port=args.port,
        data_root=args.data_root or None,
    )
    try:
        await hub.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 0
    return 0


@dataclass
class _ChatPrinter:
    member_id: str
    raw: bool
    full_history: bool
    seen_snapshot: bool = False
    snapshot_ready: Optional[asyncio.Event] = None

    def print_message(self, msg: dict[str, Any]) -> None:
        if self.raw:
            _print_raw_server_message(msg)
            if msg.get("type") == "snapshot" and not msg.get("sync") and self.snapshot_ready is not None:
                self.snapshot_ready.set()
            return
        msg_type = msg.get("type")
        if msg_type == "event":
            line = format_chat_event(msg, member_id=self.member_id)
            if line:
                print(line, flush=True)
            return
        if msg_type == "snapshot":
            if msg.get("sync"):
                count = len(msg.get("events") or [])
                print(f"(synced {count} missed event{'s' if count != 1 else ''})", flush=True)
                for event in msg.get("events") or []:
                    line = format_chat_event(event, member_id=self.member_id)
                    if line:
                        print(line, flush=True)
                return
            if self.seen_snapshot:
                return
            self.seen_snapshot = True
            present = msg.get("present") or []
            events = msg.get("events") or []
            world_name = str(msg.get("name") or msg.get("world_id") or "world")
            for line in format_join_intro(
                world_name=world_name,
                present=present,
                member_id=self.member_id,
                event_count=len(events),
                full_history=self.full_history,
            ):
                print(line, flush=True)
            if self.full_history:
                for event in events:
                    formatted = format_chat_event(event, member_id=self.member_id)
                    if formatted:
                        print(formatted, flush=True)
            if self.snapshot_ready is not None:
                self.snapshot_ready.set()
            return
        if msg_type == "error":
            code = msg.get("code")
            prefix = f"{code}: " if code else ""
            print(f"! error: {prefix}{msg.get('message')}", file=sys.stderr, flush=True)
            return
        if msg_type == "lagged":
            print(f"! lagged; sync after {msg.get('after_seq')}", file=sys.stderr, flush=True)
            return
        print(json.dumps(msg, ensure_ascii=False), flush=True)


async def _cmd_join(args: argparse.Namespace) -> int:
    from .client import MemberTakenError, WorldClient

    member_id = args.member_id
    name = args.name or member_id
    url = _ws_url(args)
    snapshot_ready = asyncio.Event()
    printer = _ChatPrinter(
        member_id=member_id,
        raw=bool(getattr(args, "raw", False)),
        full_history=bool(getattr(args, "full_history", False)),
        snapshot_ready=snapshot_ready,
    )

    print(f"Connecting as {name} …", flush=True)
    try:
        return await _run_join_session(url, member_id=member_id, name=name, printer=printer, snapshot_ready=snapshot_ready)
    except MemberTakenError:
        print(
            f"! '{member_id}' is already present in this world from another connection. "
            "Pick another --member-id, or leave from where you are already present.",
            file=sys.stderr,
            flush=True,
        )
        return 1


async def _run_join_session(
    url: str,
    *,
    member_id: str,
    name: str,
    printer: "_ChatPrinter",
    snapshot_ready: asyncio.Event,
) -> int:
    from .client import WorldClient

    async with WorldClient(url, member_id=member_id, display_name=name) as client:
        welcome = client.welcome or {}
        if welcome.get("type") != "welcome":
            print(f"unexpected: {welcome}", file=sys.stderr)
            return 1
        await client.join()

        stop = asyncio.Event()

        async def event_printer() -> None:
            try:
                async for msg in client.events():
                    printer.print_message(msg)
            except Exception:
                pass
            finally:
                stop.set()

        printer_task = asyncio.create_task(event_printer())
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(snapshot_ready.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            print("! timed out waiting for world snapshot", file=sys.stderr, flush=True)
            return 1

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
                    await client.leave()
                    print("· you left (still connected; /quit to exit)", flush=True)
                    continue
                await client.speak(text)
                if not printer.raw:
                    print("… waiting for a reply", flush=True)
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
    url = _ws_url(args)

    async with WorldClient(url, member_id=member_id, display_name=name) as client:
        print(json.dumps(client.welcome, ensure_ascii=False), flush=True)
        await client.join()
        while True:
            msg = await client.recv(timeout=5.0)
            print(json.dumps(msg, ensure_ascii=False), flush=True)
            if msg.get("type") == "snapshot":
                break

        for line in args.lines:
            await asyncio.sleep(args.delay)
            await client.speak(line)

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
            await client.leave()
            try:
                msg = await client.recv(timeout=1.0)
                print(json.dumps(msg, ensure_ascii=False), flush=True)
            except asyncio.TimeoutError:
                pass
    return 0


def _print_raw_server_message(msg: dict[str, Any]) -> None:
    msg_type = msg.get("type")
    if msg_type == "event":
        kind = msg.get("kind")
        actor = msg.get("actor_id")
        text = msg.get("text")
        seq = msg.get("seq")
        if kind in {"join", "leave"} and not text:
            print(f"[{seq}] {kind} {actor}", flush=True)
            return
        print(f"[{seq}] {kind} {actor}: {text}", flush=True)
        return
    if msg_type == "snapshot":
        if msg.get("sync"):
            extra = ""
            if msg.get("has_more"):
                extra = f", has_more next={msg.get('next_after_seq')}"
            print(
                f"(sync after {msg.get('after_seq')}: "
                f"{len(msg.get('events') or [])} events{extra})",
                flush=True,
            )
            return
        print(f"--- {msg.get('name') or 'world'} ---", flush=True)
        present = msg.get("present") or []
        names = [f"{p.get('display_name')}({p.get('member_id')})" for p in present]
        print(f"present: {', '.join(names) if names else '(empty)'}", flush=True)
        for event in msg.get("events") or []:
            text = event.get("text")
            kind = event.get("kind")
            suffix = f": {text}" if text else ""
            print(
                f"  [{event.get('seq')}] "
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
            f"! lagged; sync after {msg.get('after_seq')}",
            file=sys.stderr,
            flush=True,
        )
        return
    print(json.dumps(msg, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
