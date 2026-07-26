"""CLI client for the single local xAgent runtime."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from ...core.runtime.client import RuntimeClient, RuntimeUnavailable
from ...core.runtime.launcher import RuntimeLaunchError, RuntimeLauncher
from ...core.runtime.types import LOCAL_OWNER_PERSON_ID
from ...settings import XAgentSettings
from .paths import runtime_dir


def _config_dir(args: argparse.Namespace) -> Path:
    return runtime_dir(args)


def _ensure_runtime(
    args: argparse.Namespace,
    *,
    announce: bool = True,
) -> int:
    launcher = RuntimeLauncher(_config_dir(args))
    try:
        outcome = launcher.start()
    except RuntimeLaunchError as exc:
        print(f"Failed to start xAgent runtime: {exc}")
        return 1
    if announce:
        if outcome.state == "already_running":
            print(f"xAgent runtime is already running (pid={outcome.pid}).")
        else:
            print(f"Started xAgent runtime (pid={outcome.pid}).")
    return 0


def handle_runtime_foreground(args: argparse.Namespace) -> int:
    try:
        asyncio.run(RuntimeLauncher(_config_dir(args)).run_foreground())
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"Runtime failed: {exc}")
        return 1
    return 0


def handle_runtime_start(args: argparse.Namespace) -> int:
    return _ensure_runtime(args)


def handle_runtime_stop(args: argparse.Namespace) -> int:
    launcher = RuntimeLauncher(_config_dir(args))
    try:
        outcome = launcher.stop()
    except RuntimeLaunchError as exc:
        print(f"Failed to stop xAgent runtime: {exc}")
        return 1
    if outcome.state == "already_stopped":
        print("xAgent runtime is not running.")
        return 0
    print(f"Stopped xAgent runtime (pid={outcome.pid}).")
    return 0


def handle_runtime_restart(args: argparse.Namespace) -> int:
    try:
        outcome = RuntimeLauncher(_config_dir(args)).restart()
    except RuntimeLaunchError as exc:
        print(f"Failed to restart xAgent runtime: {exc}")
        return 1
    print(f"Restarted xAgent runtime (pid={outcome.pid}).")
    return 0


def handle_runtime_status(args: argparse.Namespace) -> int:
    try:
        status = RuntimeLauncher(_config_dir(args)).status()
    except RuntimeLaunchError as exc:
        print(f"Runtime status failed: {exc}")
        return 1
    if status is None:
        status = {"running": False, "channels": []}
    if getattr(args, "json_output", False):
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0
    if not status.get("running"):
        print("Runtime: stopped")
        return 0
    print(f"Runtime: running (pid={status['pid']})")
    for channel in status.get("channels", []):
        error = f" error={channel['error']}" if channel.get("error") else ""
        print(
            f"  {channel['name']}: {channel['state']} "
            f"(enabled={str(channel['enabled']).lower()}){error}"
        )
    return 0


def _write_channel_enabled(config_dir: Path, name: str, enabled: bool) -> None:
    path = config_dir / "config.yaml"
    settings = XAgentSettings.load(path)
    settings.with_channel_enabled(name, enabled).write_atomic(path)


def handle_channel(args: argparse.Namespace) -> int:
    root = _config_dir(args)
    client = RuntimeClient(root)
    action = args.channel_action
    name = getattr(args, "name", None)

    if action == "list":
        try:
            payload = client.request("GET", "/v1/channels")
            channels = payload["channels"]
        except RuntimeUnavailable:
            settings = XAgentSettings.load(root / "config.yaml")
            channels = [
                {
                    "name": channel_name,
                    "enabled": getattr(settings.channels, channel_name).enabled,
                    "state": "runtime-stopped",
                    "error": "",
                }
                for channel_name in ("api", "feishu", "weixin", "voice")
            ]
        if getattr(args, "json_output", False):
            print(json.dumps({"channels": channels}, ensure_ascii=False, indent=2))
        else:
            for channel in channels:
                print(
                    f"{channel['name']}: {channel['state']} "
                    f"(enabled={str(channel['enabled']).lower()})"
                )
        return 0

    if action == "setup":
        return _handle_channel_setup(args)

    if name not in {"api", "feishu", "weixin", "voice"}:
        print(f"Unknown channel: {name}")
        return 1

    if action in {"start", "restart"} and _ensure_runtime(args, announce=False) != 0:
        return 1
    try:
        payload = client.request("POST", f"/v1/channels/{name}/{action}")
    except RuntimeUnavailable as exc:
        if action == "stop":
            _write_channel_enabled(root, name, False)
            print(f"{name}: stopped (runtime is not running)")
            return 0
        print(f"Channel {action} failed: {exc}")
        return 1
    except RuntimeError as exc:
        print(f"Channel {action} failed: {exc}")
        return 1
    channel = payload["channel"]
    print(
        f"{name}: {channel['state']} "
        f"(enabled={str(channel['enabled']).lower()})"
    )
    return 0


def _handle_channel_setup(args: argparse.Namespace) -> int:
    name = args.name
    if name == "api":
        settings = XAgentSettings.load(_config_dir(args) / "config.yaml")
        data = settings.model_dump(mode="python", exclude_none=True)
        api = data["channels"]["api"]
        api["host"] = getattr(args, "host", None) or api.get("host") or "127.0.0.1"
        api["port"] = getattr(args, "port", None) or api.get("port") or 8010
        updated = XAgentSettings.model_validate(data)
        updated.write_atomic(_config_dir(args) / "config.yaml")
        print("api: configured")
        return 0

    from . import setup

    handlers = {
        "feishu": setup.handle_init_feishu,
        "weixin": setup.handle_init_weixin,
        "voice": setup.handle_init_voice,
    }
    handler = handlers.get(name)
    if handler is None:
        print(f"Unknown channel: {name}")
        return 1
    return int(handler(args) or 0)


def handle_delivery(args: argparse.Namespace) -> int:
    client = RuntimeClient(_config_dir(args))
    try:
        if args.delivery_action == "list":
            status = getattr(args, "status", None)
            query = f"?status={status}" if status else ""
            payload = client.request("GET", f"/v1/deliveries{query}")
            deliveries = payload["deliveries"]
            if getattr(args, "json_output", False):
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                for delivery in deliveries:
                    print(
                        f"{delivery['delivery_id']} {delivery['status']} "
                        f"{delivery['channel']} attempts={delivery['attempts']}"
                    )
            return 0
        payload = client.request(
            "POST",
            f"/v1/deliveries/{args.delivery_id}/retry",
        )
        print(f"{payload['delivery']['delivery_id']}: pending")
        return 0
    except (RuntimeUnavailable, RuntimeError) as exc:
        print(f"Delivery command failed: {exc}")
        return 1


def handle_person(args: argparse.Namespace) -> int:
    client = RuntimeClient(_config_dir(args))
    try:
        if args.person_action == "list":
            payload = client.request("GET", "/v1/people")
            if getattr(args, "json_output", False):
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                for person in payload["people"]:
                    accounts = ", ".join(
                        f"{item['channel']}:{item['account_id']}"
                        for item in person["accounts"]
                    )
                    print(f"{person['person_id']} {accounts}")
            return 0
        payload = client.request(
            "POST",
            f"/v1/people/{args.person_id}/accounts",
            json={
                "channel": args.channel,
                "account_id": args.account_id,
            },
        )
        account = payload["account"]
        print(
            f"linked {account['channel']}:{account['account_id']} "
            f"to {account['person_id']}"
        )
        return 0
    except (RuntimeUnavailable, RuntimeError) as exc:
        print(f"Person command failed: {exc}")
        return 1


def handle_chat(args: argparse.Namespace) -> int:
    if _ensure_runtime(args, announce=False) != 0:
        return 1
    client = RuntimeClient(_config_dir(args))
    def send(text: str) -> int:
        try:
            payload = client.request(
                "POST",
                "/v1/events",
                json={
                    "kind": "chat",
                    "source": "cli",
                    "conversation_id": "cli:main",
                    "speaker_id": LOCAL_OWNER_PERSON_ID,
                    "audience_ids": [LOCAL_OWNER_PERSON_ID],
                    "content": text,
                    "metadata": {"stream": False},
                    "wait": True,
                },
            )
        except (RuntimeUnavailable, RuntimeError) as exc:
            print(f"Chat failed: {exc}")
            return 1
        final = _final_text(payload.get("result") or {})
        print(final)
        return 0

    if args.message is not None:
        return send(args.message)
    print("xAgent chat. Type /exit to leave.")
    while True:
        try:
            text = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if text in {"/exit", "/quit"}:
            return 0
        if text:
            if send(text) != 0:
                return 1


def _final_text(result: dict[str, Any]) -> str:
    final = ""
    for item in result.get("events", []):
        if item.get("type") == "message_done" and item.get("phase") == "final":
            final = str(item.get("content") or "")
        elif item.get("type") in {"text", "message"}:
            final = str(item.get("content") or final)
    return final
