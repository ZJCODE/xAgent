"""Manage one authoritative runtime per agent and connect local clients."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

from ...core.runtime.client import RuntimeClient, RuntimeUnavailable
from ...core.runtime.ownership import runtime_is_active, runtime_paths
from .channels import VALID_CHANNELS, load_config_file, normalize_channel_values, restart_channels_from_status
from .paths import runtime_dir
from .processes import stop_managed_process, tail_text

START_TIMEOUT = 30.0
STOP_TIMEOUT = 35.0


def _runtime_ready(root: Path) -> bool:
    import httpx
    try:
        return bool(asyncio.run(RuntimeClient(root).readiness()).get("runtime_ready"))
    except RuntimeUnavailable:
        return False
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            # A running older version can still be managed during upgrades.
            return bool(asyncio.run(runtime_status(root)).get("runtime_ready"))
        raise


def selected_channels(args: argparse.Namespace) -> list[str] | None:
    if getattr(args, "no_channels", False):
        return []
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
        # Ownership can disappear while the status request is in flight.
        if not runtime_is_active(root):
            return {"status": "stopped", "runtime_running": False, "channels": {}}
        try:
            cached = json.loads(runtime_paths(root).state_path.read_text())
        except (OSError, ValueError):
            cached = {}
        stopping = isinstance(exc, RuntimeUnavailable) and cached.get("state") in {"stopping", "stopped"}
        if stopping:
            cached.pop("error", None)
        return {**cached, "status": "stopping" if stopping else cached.get("state", "starting"), "runtime_running": True,
                "runtime_ready": False, **({} if stopping else {"error": str(exc)}), "channels": {
                    name: {**row, "status": row.get("state", "unknown")}
                    for name, row in cached.get("channels", {}).items()}}


def start_runtime(root: Path, channels: list[str] | None = None) -> dict:
    root = root.expanduser().resolve()
    paths = runtime_paths(root)
    if runtime_is_active(root):
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if _runtime_ready(root):
                return asyncio.run(runtime_status(root))
            time.sleep(0.1)
        raise RuntimeError("Agent owns its runtime but is not ready; inspect xagent logs.")
    paths.log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "xagent.interfaces.cli", "_run-agent", "--config-dir", str(root)]
    if channels == []:
        command.append("--no-channels")
    elif channels is not None:
        command += ["--channels", ",".join(channels)]
    with paths.log_path.open("ab") as output:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        if runtime_is_active(root) and _runtime_ready(root):
            return asyncio.run(runtime_status(root))
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


async def stop_runtime(root: Path, *, restart: bool = False) -> dict:
    if not runtime_is_active(root):
        return {"status": "stopped", "runtime_running": False}
    stopped = {"status": "stopped", "runtime_running": False}
    try:
        client = RuntimeClient(root)
        if restart:
            response = await client.stop(restart=True)
            if "restart_channels" in response:
                stopped["restart_channels"] = response["restart_channels"]
        else:
            await client.stop()
    except RuntimeUnavailable:
        # Shutdown may close the socket before the stop acknowledgement arrives.
        status = await runtime_status(root)
        if not status.get("runtime_running"):
            return stopped
        if status.get("status") != "stopping":
            raise
    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline:
        if not runtime_is_active(root):
            return stopped
        await asyncio.sleep(0.1)
    raise RuntimeError("Agent did not release runtime ownership within 35 seconds; inspect its logs.")


async def restart_runtime(root: Path, channels: list[str] | None = None) -> dict:
    """Restart one owner and restore only this operation's live connection intent."""
    import httpx

    root = root.expanduser().resolve()
    was_active = runtime_is_active(root)
    snapshot = None
    warnings = []
    if was_active and channels is None:
        try:
            snapshot = restart_channels_from_status(await RuntimeClient(root).status())
        except (RuntimeUnavailable, httpx.HTTPStatusError):
            pass  # The stop acknowledgement may still supply an authoritative snapshot.
    try:
        stopped = await stop_runtime(root, restart=True)
    except RuntimeUnavailable:
        # Retain recovery for an unreachable older core, without using cached
        # channel state. A real drain/cleanup failure must never reach this path.
        ok, message = await asyncio.to_thread(stop_managed_process, runtime_paths(root).pid_path)
        if not ok:
            raise RuntimeError(message)
        if runtime_is_active(root):
            raise RuntimeError("Agent has not released runtime ownership; inspect its logs.")
        stopped = {}

    if channels is None:
        acknowledged = stopped.get("restart_channels")
        if isinstance(acknowledged, list) and all(isinstance(name, str) and name in VALID_CHANNELS for name in acknowledged):
            snapshot = list(dict.fromkeys(acknowledged))
        if snapshot is not None:
            configured = load_config_file(root).get("channels", {})
            configured = configured if isinstance(configured, Mapping) else {}
            channels = []
            for name in snapshot:
                settings = configured.get(name, {})
                if isinstance(settings, Mapping) and settings.get("enabled") is False:
                    warnings.append(f"{name}: connection not restored because it is disabled in the new configuration.")
                else:
                    channels.append(name)
        elif was_active:
            warnings.append("Previous connections could not be read; the Agent started with its default connections.")

    result = await asyncio.to_thread(start_runtime, root, channels)
    for name, row in result.get("channels", {}).items():
        if (row.get("state") or row.get("status")) in {"failed", "error"}:
            warnings.append(f"{name}: connection failed; check channel status and runtime logs.")
    if warnings:
        result = {**result, "restart_warnings": warnings}
    return result


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
    try:
        root = runtime_dir(args)
        result = asyncio.run(restart_runtime(root, selected_channels(args)))
        print("Agent restarted. Check channel status for connection progress.")
        for name, row in result.get("channels", {}).items():
            print(f"  {name}: {row.get('state') or row.get('status', 'unknown')}")
        for warning in result.get("restart_warnings", []):
            print(f"Warning: {warning}")
        print(f"Logs: {runtime_paths(root).log_path}")
        return 0
    except Exception as exc:
        print(f"Cannot restart Agent: {exc}")
        return 1


def handle_channel_connection(args: argparse.Namespace) -> int:
    async def control():
        client = RuntimeClient(runtime_dir(args))
        action = args.channel_action
        method = client.connect_channel if action == "connect" else client.disconnect_channel
        return await method(args.channel)
    try:
        result = asyncio.run(control())
        print(f"{args.channel}: {result['channel']['state']}")
        return 0
    except Exception as exc:
        import httpx
        if isinstance(exc, httpx.HTTPStatusError):
            try:
                detail = exc.response.json().get("detail", "Channel operation failed")
            except ValueError:
                detail = "Channel operation failed"
        else:
            detail = str(exc)
        print(f"Cannot {args.channel_action} {args.channel}: {detail}")
        return 1


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
