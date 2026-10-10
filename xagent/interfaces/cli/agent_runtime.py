"""Manage one authoritative runtime per agent and connect local clients."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from ...core.runtime.client import RuntimeClient
from ...core.runtime.ownership import runtime_is_active, runtime_paths
from .channels import normalize_channel_values
from .paths import runtime_dir
from .processes import tail_text

START_TIMEOUT = 30.0
STOP_TIMEOUT = 35.0


def selected_channels(args: argparse.Namespace) -> list[str] | None:
    values = getattr(args, "channels", None)
    if not values:
        return None
    return normalize_channel_values(values if isinstance(values, list) else [values], default="api")


async def runtime_status(root: Path) -> dict:
    if not runtime_is_active(root):
        return {"status": "stopped", "runtime_running": False, "channels": {}}
    try:
        result = await RuntimeClient(root).status()
        channels = {name: {**row, "status": row.get("status") or row.get("state", "unknown")}
                    for name, row in result.get("channels", {}).items()}
        return {**result, "status": result.get("status") or result.get("state", "starting"),
                "channels": channels, "memory": result.get("journal", {}), "runtime_running": True}
    except Exception as exc:
        try:
            cached = json.loads(runtime_paths(root).state_path.read_text())
        except (OSError, ValueError):
            cached = {}
        return {**cached, "status": cached.get("state", "starting"), "runtime_running": True,
                "runtime_ready": False, "error": str(exc), "channels": {
                    name: {**row, "status": row.get("state", "unknown")}
                    for name, row in cached.get("channels", {}).items()}}


def start_runtime(root: Path, channels: list[str] | None = None) -> dict:
    root = root.expanduser().resolve()
    paths = runtime_paths(root)
    if runtime_is_active(root):
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            status = asyncio.run(runtime_status(root))
            if status.get("runtime_ready"):
                return status
            time.sleep(0.1)
        raise RuntimeError("Agent owns its runtime but is not ready; inspect xagent logs.")
    paths.log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "xagent.interfaces.cli", "_run-agent", "--config-dir", str(root)]
    if channels is not None:
        command += ["--channels", ",".join(channels)]
    with paths.log_path.open("ab") as output:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        status = asyncio.run(runtime_status(root))
        if status.get("runtime_running") and status.get("runtime_ready"):
            return status
        if process.poll() is not None:
            # Another concurrent launcher can win ownership of the same agent.
            if runtime_is_active(root):
                time.sleep(0.1)
                continue
            raise RuntimeError(tail_text(paths.log_path) or f"Agent exited during startup ({process.returncode}).")
        time.sleep(0.1)
    # The child may still be recovering data; ownership prevents a second core.
    raise RuntimeError("Agent startup timed out; inspect xagent status and logs before retrying.")


async def ensure_runtime(root: Path) -> RuntimeClient:
    await asyncio.to_thread(start_runtime, root)
    client = RuntimeClient(root)
    await client.status()
    return client


async def stop_runtime(root: Path) -> dict:
    if not runtime_is_active(root):
        return {"status": "stopped", "runtime_running": False}
    await RuntimeClient(root).stop()
    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline:
        if not runtime_is_active(root):
            return {"status": "stopped", "runtime_running": False}
        await asyncio.sleep(0.1)
    raise RuntimeError("Agent did not release runtime ownership within 35 seconds; inspect its logs.")


def handle_run(args: argparse.Namespace) -> int:
    from ...core.runtime.host import RuntimeHost
    try:
        asyncio.run(RuntimeHost(runtime_dir(args), channels=selected_channels(args)).run())
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"Cannot run Agent: {exc}")
        return 1


def handle_start(args: argparse.Namespace) -> int:
    try:
        status = start_runtime(runtime_dir(args), selected_channels(args))
        print(f"Agent: {status.get('status', 'running')}")
        print(f"Logs: {runtime_paths(runtime_dir(args)).log_path}")
        return 0
    except Exception as exc:
        print(f"Cannot start Agent: {exc}")
        return 1


def handle_stop(args: argparse.Namespace) -> int:
    try:
        result = asyncio.run(stop_runtime(runtime_dir(args)))
        print(f"Agent: {result['status']}")
        return 0
    except Exception as exc:
        print(f"Cannot stop Agent: {exc}")
        return 1


def handle_restart(args: argparse.Namespace) -> int:
    return handle_start(args) if handle_stop(args) == 0 else 1


def handle_status(args: argparse.Namespace) -> int:
    status = asyncio.run(runtime_status(runtime_dir(args)))
    if getattr(args, "json_output", False):
        print(json.dumps(status, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"Agent: {status.get('status', 'unknown')}")
        for name, detail in status.get("channels", {}).items():
            print(f"  {name}: {detail.get('status', 'unknown') if isinstance(detail, dict) else detail}")
        if status.get("error"):
            print(status["error"])
        if status.get("needs_restart"):
            print("Configuration changed; restart the Agent to apply it.")
        if status.get("memory"):
            print(f"Memory: {json.dumps(status['memory'], ensure_ascii=False, default=str)}")
        if status.get("tasks"):
            print(f"Tasks: {json.dumps(status['tasks'], ensure_ascii=False, default=str)}")
    return 0


def handle_logs(args: argparse.Namespace) -> int:
    path = runtime_paths(runtime_dir(args)).log_path
    output = tail_text(path, max_lines=max(1, int(getattr(args, "lines", 80))))
    print(output or f"No log output: {path}")
    if getattr(args, "follow", False):
        from .runtime import _follow_log
        try:
            _follow_log(path)
        except KeyboardInterrupt:
            pass
    return 0
