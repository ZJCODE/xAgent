"""CLI handlers for scanning and restarting all managed xAgent processes."""

from __future__ import annotations

import argparse
import json
from typing import Any

from .processes import (
    ManagedProcessRef,
    iter_managed_process_refs,
    iter_running_process_refs,
    process_status_row,
    stop_managed_process,
)
from .runtime import _start_background_channel, _start_background_web
from .web_client import web_client_paths


def _format_process_label(row: dict[str, Any]) -> str:
    if row["scope"] == "web":
        return "web"
    agent = row.get("agent", "?")
    channel = row.get("channel", "?")
    return f"{agent}/{channel}"


def handle_processes_status(args: argparse.Namespace) -> int:
    rows = [process_status_row(ref) for ref in iter_managed_process_refs()]
    running_count = sum(1 for row in rows if row["status"] == "running")
    payload = {"processes": rows, "running_count": running_count}

    if getattr(args, "json_output", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 2 if running_count > 0 else 0

    if not rows:
        print("No managed processes found.")
        return 0

    for row in rows:
        pid = row["pid"]
        pid_text = f" pid={pid}" if pid is not None else ""
        print(f"{_format_process_label(row)}: {row['status']}{pid_text}")
        print(f"  pid: {row['pid_path']}")
        print(f"  log: {row['log_path']}")

    return 2 if running_count > 0 else 0


def _channel_restart_namespace(ref: ManagedProcessRef) -> argparse.Namespace:
    return argparse.Namespace(
        agent=ref.agent,
        config_dir=str(ref.config_dir) if ref.config_dir is not None else None,
        channels=[ref.channel],
        host=None,
        port=None,
        open_browser=False,
        max_concurrent_chats=None,
        queue_timeout=None,
        chat_timeout=None,
        user_id=None,
        verbose=False,
        input_device=None,
        output_device=None,
        api_url=None,
    )


def handle_processes_restart(args: argparse.Namespace) -> int:
    running_refs = list(iter_running_process_refs())
    if not running_refs:
        if getattr(args, "json_output", False):
            print(json.dumps({"restarted": [], "running_count": 0}, indent=2, sort_keys=True))
        else:
            print("No running managed processes to restart.")
        return 0

    restarted: list[dict[str, Any]] = []
    ok = True

    for ref in running_refs:
        label = "web" if ref.scope == "web" else f"{ref.agent}/{ref.channel}"

        if ref.scope == "web":
            paths = web_client_paths()
            stopped, message = stop_managed_process(paths.pid_path)
            if not stopped:
                ok = False
                restarted.append({"label": label, "scope": ref.scope, "ok": False, "message": message})
                if not getattr(args, "json_output", False):
                    print(f"{label}: {message}")
                continue

            started, _already_running = _start_background_web(args)
            entry_ok = started
            message = "restarted" if started else "failed to restart"
        else:
            stopped, message = stop_managed_process(ref.pid_path)
            if not stopped:
                ok = False
                restarted.append({
                    "label": label,
                    "scope": ref.scope,
                    "agent": ref.agent,
                    "channel": ref.channel,
                    "ok": False,
                    "message": message,
                })
                if not getattr(args, "json_output", False):
                    print(f"{label}: {message}")
                continue

            restart_args = _channel_restart_namespace(ref)
            entry_ok = _start_background_channel(
                restart_args,
                channel=str(ref.channel),
                config_dir=ref.config_dir,
            )
            message = "restarted" if entry_ok else "failed to restart"

        ok = ok and entry_ok
        restarted.append({
            "label": label,
            "scope": ref.scope,
            "agent": ref.agent,
            "channel": ref.channel,
            "ok": entry_ok,
            "message": message,
        })
        if not getattr(args, "json_output", False):
            print(f"{label}: {message}")

    if getattr(args, "json_output", False):
        print(
            json.dumps(
                {"restarted": restarted, "running_count": len(running_refs)},
                indent=2,
                sort_keys=True,
            )
        )

    return 0 if ok else 1
