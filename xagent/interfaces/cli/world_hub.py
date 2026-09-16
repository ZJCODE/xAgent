"""Machine-level world hub: config, process paths, and CLI handlers.

The hub is a venue, not a channel. One process on this machine hosts every
world; agent presence is a separate relationship that talks to a running
API process.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import quote, urlparse

from agents_world import DEFAULT_HOST, DEFAULT_PORT

from ...integrations.world.presence import (
    mark_world_left,
    mark_world_presence,
    read_world_presence,
    world_id_from_url,
)
from .agents import AgentRegistryError, load_agent_registry_or_empty, management_root, resolve_agent_name
from .channels import CHANNEL_API, api_config, load_config_file
from .paths import runtime_dir
from .processes import (
    ManagedProcessPaths,
    managed_paths,
    running_pid,
    start_background,
    stop_managed_process,
    tail_text,
)


DEFAULT_WORLD_HOST = DEFAULT_HOST
DEFAULT_WORLD_PORT = DEFAULT_PORT
HUB_READY_TIMEOUT = 5.0
API_READY_TIMEOUT = 8.0
HTTP_TIMEOUT = 2.0


def world_hub_runtime_root() -> Path:
    return management_root()


def world_hub_paths(*, root: Optional[Path] = None) -> ManagedProcessPaths:
    runtime_root = (root or world_hub_runtime_root()).expanduser().resolve()
    return ManagedProcessPaths(
        pid_path=runtime_root / "run" / "world.pid",
        log_path=runtime_root / "logs" / "world.log",
    )


def world_hub_config(*, root: Optional[Path] = None) -> dict[str, Any]:
    """Return machine-level hub settings from ``~/.xagent/config.yaml`` when present."""
    runtime_root = (root or world_hub_runtime_root()).expanduser().resolve()
    data = load_config_file(runtime_root)
    world_cfg = data.get("world") if isinstance(data, Mapping) else None
    world_cfg = dict(world_cfg) if isinstance(world_cfg, Mapping) else {}
    host = str(world_cfg.get("host") or DEFAULT_WORLD_HOST).strip() or DEFAULT_WORLD_HOST
    try:
        port = int(world_cfg.get("port") or DEFAULT_WORLD_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_WORLD_PORT
    return {
        "enabled": bool(world_cfg.get("enabled", True)),
        "host": host,
        "port": port,
    }


def world_agent_config(config: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    world_cfg = config.get("world") if isinstance(config, Mapping) else None
    world_cfg = dict(world_cfg) if isinstance(world_cfg, Mapping) else {}
    return {
        "autojoin": bool(world_cfg.get("autojoin", True)),
    }


def world_hub_browse_host(host: str) -> str:
    raw = str(host or DEFAULT_WORLD_HOST).strip() or DEFAULT_WORLD_HOST
    if raw in {"0.0.0.0", "::"}:
        return "127.0.0.1"
    return raw


def world_hub_public_url(*, host: Optional[str] = None, port: Optional[int] = None, root: Optional[Path] = None) -> str:
    cfg = world_hub_config(root=root)
    browse_host = world_hub_browse_host(host or cfg["host"])
    resolved_port = int(port if port is not None else cfg["port"])
    if ":" in browse_host and not browse_host.startswith("["):
        browse_host = f"[{browse_host}]"
    return f"http://{browse_host}:{resolved_port}"


def world_ws_url(world_id: str, *, host: Optional[str] = None, port: Optional[int] = None, root: Optional[Path] = None) -> str:
    http_url = world_hub_public_url(host=host, port=port, root=root)
    parsed = urlparse(http_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}/ws/{quote(str(world_id).strip(), safe='')}"


def world_hub_is_running(*, root: Optional[Path] = None) -> bool:
    return running_pid(world_hub_paths(root=root).pid_path) is not None


def agent_api_public_url(config_dir: Path) -> str:
    config = load_config_file(config_dir)
    api_cfg = api_config(config)
    host = str(api_cfg.get("host") or "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(api_cfg.get("port") or 8010)
    except (TypeError, ValueError):
        port = 8010
    browse_host = world_hub_browse_host(host)
    if ":" in browse_host and not browse_host.startswith("["):
        browse_host = f"[{browse_host}]"
    return f"http://{browse_host}:{port}"


def _http_json(
    method: str,
    url: str,
    *,
    body: Optional[dict[str, Any]] = None,
    timeout: float = HTTP_TIMEOUT,
) -> tuple[int, dict[str, Any], str]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            payload: Any = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(payload, dict):
                payload = {"data": payload}
            return int(response.status), payload, ""
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return int(exc.code), payload, str(exc.reason or exc)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return 0, {}, str(exc)


def wait_http_ok(url: str, *, timeout: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        status, _payload, _error = _http_json("GET", url, timeout=min(0.6, timeout or 0.6))
        if 200 <= status < 300:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.15)


def wait_for_world_hub(*, host: Optional[str] = None, port: Optional[int] = None, timeout: float = HUB_READY_TIMEOUT) -> bool:
    url = world_hub_public_url(host=host, port=port).rstrip("/") + "/worlds"
    return wait_http_ok(url, timeout=timeout)


def wait_for_agent_world_api(config_dir: Path, *, timeout: float = API_READY_TIMEOUT) -> bool:
    url = agent_api_public_url(config_dir).rstrip("/") + "/world/status"
    return wait_http_ok(url, timeout=timeout)


def list_world_summaries_from_disk(*, root: Optional[Path] = None) -> list[dict[str, Any]]:
    from agents_world.paths import list_world_ids, world_data_dir
    from agents_world.store import WorldStore

    data_root = (root or world_hub_runtime_root()).expanduser().resolve()
    summaries: list[dict[str, Any]] = []
    for world_id in list_world_ids(root=data_root):
        db_path = world_data_dir(world_id, root=data_root) / "world.sqlite3"
        store = WorldStore(db_path, world_id=world_id)
        try:
            summaries.append(
                {
                    "id": world_id,
                    "name": store.name,
                    "latest_seq": store.max_seq(),
                    "present_count": len(store.list_present()),
                }
            )
        finally:
            store.close()
    return summaries


def fetch_hub_worlds(*, host: Optional[str] = None, port: Optional[int] = None) -> Optional[list[dict[str, Any]]]:
    url = world_hub_public_url(host=host, port=port).rstrip("/") + "/worlds"
    status, payload, _error = _http_json("GET", url)
    if not (200 <= status < 300):
        return None
    worlds = payload.get("worlds")
    if not isinstance(worlds, list):
        return []
    return [item for item in worlds if isinstance(item, dict)]


def list_world_summaries(*, host: Optional[str] = None, port: Optional[int] = None, root: Optional[Path] = None) -> list[dict[str, Any]]:
    live = fetch_hub_worlds(host=host, port=port)
    if live is not None:
        return live
    return list_world_summaries_from_disk(root=root)


def create_world_on_hub(name: str, *, host: Optional[str] = None, port: Optional[int] = None) -> tuple[int, dict[str, Any], str]:
    url = world_hub_public_url(host=host, port=port).rstrip("/") + f"/worlds/create?name={quote(name)}"
    return _http_json("GET", url, timeout=5.0)


def create_world_on_disk(name: str, *, root: Optional[Path] = None) -> tuple[int, dict[str, Any], str]:
    from agents_world.config import WorldConfig
    from agents_world.paths import allocate_world_id, list_world_ids, world_data_dir
    from agents_world.store import open_store_for_world

    data_root = (root or world_hub_runtime_root()).expanduser().resolve()
    label = str(name or "").strip()
    if not label:
        return 1, {}, "name is required"
    try:
        world_id = allocate_world_id(label, taken=list_world_ids(root=data_root))
        config = WorldConfig.create(world_id=world_id, name=label)
    except ValueError as exc:
        return 1, {}, str(exc)
    if (world_data_dir(config.world_id, root=data_root) / "world.sqlite3").is_file():
        return 1, {}, f"world already exists: {config.world_id}"
    store = open_store_for_world(config, root=data_root)
    store.close()
    return 0, {"id": config.world_id, "name": config.name}, ""


def delete_world_on_hub(world_id: str, *, host: Optional[str] = None, port: Optional[int] = None) -> tuple[int, dict[str, Any], str]:
    encoded = quote(str(world_id).strip(), safe="")
    url = (
        world_hub_public_url(host=host, port=port).rstrip("/")
        + f"/worlds/{encoded}/delete?confirm={encoded}"
    )
    return _http_json("GET", url, timeout=5.0)


def delete_world_on_disk(world_id: str, *, root: Optional[Path] = None) -> tuple[int, dict[str, Any], str]:
    from agents_world.paths import remove_world_dir, validate_world_id

    data_root = (root or world_hub_runtime_root()).expanduser().resolve()
    label = str(world_id or "").strip()
    if not label:
        return 1, {}, "world id is required"
    try:
        wid = validate_world_id(label)
        removed = remove_world_dir(wid, root=data_root)
    except FileNotFoundError as exc:
        return 1, {}, str(exc)
    except ValueError as exc:
        return 1, {}, str(exc)
    return 0, {"id": wid, "deleted": True, "path": str(removed)}, ""


def _confirm_world_delete(world_id: str, path: Path, *, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("Refusing to delete without confirmation. Re-run with --yes to confirm.")
        return False
    prompt = (
        f"Remove world {world_id!r} and delete all data at:\n"
        f"{path}\n"
        "This removes the event log and spoken files. Anyone present will be disconnected."
    )
    answer = input(f"{prompt}\nType {world_id!r} to confirm: ").strip()
    return answer == world_id


def clear_local_presence_for_world(world_id: str) -> list[str]:
    """Stop local agents from auto-rejoining a world that no longer exists."""
    cleared: list[str] = []
    registry = load_agent_registry_or_empty()
    for name, entry in sorted(registry.agents.items()):
        presence = read_world_presence(entry.path)
        if not presence:
            continue
        recorded = str(presence.get("world_id") or "").strip() or world_id_from_url(
            str(presence.get("world_url") or "")
        )
        if recorded != world_id:
            continue
        if presence.get("want_present") and running_pid(managed_paths(entry.path, CHANNEL_API).pid_path) is not None:
            _http_json(
                "POST",
                agent_api_public_url(entry.path).rstrip("/") + "/world/leave",
                timeout=2.0,
            )
        mark_world_left(entry.path)
        cleared.append(name)
    return cleared


def _resolved_hub_bind(args: argparse.Namespace) -> tuple[str, int]:
    cfg = world_hub_config()
    host = str(getattr(args, "host", None) or cfg["host"] or DEFAULT_WORLD_HOST).strip() or DEFAULT_WORLD_HOST
    port_value = getattr(args, "port", None)
    port = int(port_value if port_value is not None else cfg["port"])
    return host, port


def _world_command(args: argparse.Namespace) -> list[str]:
    host, port = _resolved_hub_bind(args)
    command = [
        sys.executable,
        "-m",
        "xagent.interfaces.cli",
        "_run-world",
        "--host",
        host,
        "--port",
        str(port),
        "--data-root",
        str(world_hub_runtime_root()),
    ]
    if getattr(args, "open_browser", False):
        command.append("--open")
    return command


def _start_background_world(args: argparse.Namespace) -> tuple[bool, bool]:
    paths = world_hub_paths()
    result = start_background(
        _world_command(args),
        pid_path=paths.pid_path,
        log_path=paths.log_path,
    )
    if result.ok:
        print(f"Started world hub in background (pid={result.pid}).")
        print(f"Logs: {paths.log_path}")
        return True, False

    if result.error.startswith("already running"):
        print(f"World hub is already running (pid={result.pid}).")
        return True, True

    print(f"Failed to start world hub: {result.error}")
    if result.recent_output:
        print(result.recent_output)
    return False, False


def restore_local_world_presence(*, host: Optional[str] = None, port: Optional[int] = None) -> list[dict[str, Any]]:
    """Re-invite local agents that still want to be present."""
    registry = load_agent_registry_or_empty()
    restored: list[dict[str, Any]] = []
    for name, entry in sorted(registry.agents.items()):
        presence = read_world_presence(entry.path)
        if not presence or not presence.get("want_present"):
            continue
        if not world_agent_config(load_config_file(entry.path)).get("autojoin", True):
            restored.append({"agent": name, "ok": False, "message": "autojoin disabled"})
            continue
        world_url = str(presence.get("world_url") or "").strip()
        if not world_url:
            world_id = str(presence.get("world_id") or "").strip()
            if world_id:
                world_url = world_ws_url(world_id, host=host, port=port)
        if not world_url:
            restored.append({"agent": name, "ok": False, "message": "no world url"})
            continue
        if running_pid(managed_paths(entry.path, CHANNEL_API).pid_path) is None:
            print(f"{name}: api channel is not running; not rejoining {presence.get('world_id') or world_url}")
            restored.append({"agent": name, "ok": False, "message": "api not running"})
            continue
        if not wait_for_agent_world_api(entry.path, timeout=2.0):
            print(f"{name}: api channel is not world-ready; not rejoining")
            restored.append({"agent": name, "ok": False, "message": "api not world-ready"})
            continue
        status, payload, error = _http_json(
            "POST",
            agent_api_public_url(entry.path).rstrip("/") + "/world/join",
            body={
                "world_url": world_url,
                "member_id": str(presence.get("member_id") or name),
                "display_name": str(presence.get("display_name") or name),
            },
            timeout=5.0,
        )
        ok = 200 <= status < 300
        world_id = str(payload.get("world_id") or presence.get("world_id") or world_id_from_url(world_url))
        message = "rejoined" if ok else (error or payload.get("detail") or payload.get("error") or f"HTTP {status}")
        if ok:
            print(f"{name}: rejoined {world_id or world_url}")
        else:
            print(f"{name}: failed to rejoin ({message})")
        restored.append({"agent": name, "ok": ok, "message": str(message), "world_id": world_id})
    return restored


def _after_hub_up(args: argparse.Namespace, *, restore: bool) -> None:
    host, port = _resolved_hub_bind(args)
    if not wait_for_world_hub(host=host, port=port):
        print("World hub did not become reachable. Check: xagent world logs")
        return
    if restore:
        restore_local_world_presence(host=host, port=port)


def handle_world_start(args: argparse.Namespace) -> int:
    cfg = world_hub_config()
    if not cfg.get("enabled", True):
        print("World hub is disabled in config (world.enabled=false).")
        return 1

    started, already_running = _start_background_world(args)
    if not started:
        return 1
    if already_running:
        if getattr(args, "open_browser", False):
            return handle_world_open(args)
        return 0
    _after_hub_up(args, restore=True)
    if getattr(args, "open_browser", False):
        return handle_world_open(args)
    return 0


def handle_world_stop(args: argparse.Namespace) -> int:
    del args
    paths = world_hub_paths()
    stopped, message = stop_managed_process(paths.pid_path)
    print(f"world: {message}")
    return 0 if stopped else 1


def handle_world_restart(args: argparse.Namespace) -> int:
    paths = world_hub_paths()
    print("Restarting the world hub disconnects everyone who is present; local agents will be invited back.")
    stopped, message = stop_managed_process(paths.pid_path)
    print(f"world: {message}")
    if not stopped:
        return 1
    started, _already_running = _start_background_world(args)
    if not started:
        return 1
    _after_hub_up(args, restore=True)
    return 0


def handle_world_status(args: argparse.Namespace) -> int:
    cfg = world_hub_config()
    host, port = _resolved_hub_bind(args)
    paths = world_hub_paths()
    pid = running_pid(paths.pid_path)
    worlds = list_world_summaries(host=host, port=port)
    row = {
        "status": "running" if pid is not None else "stopped",
        "pid": pid,
        "pid_path": str(paths.pid_path),
        "log_path": str(paths.log_path),
        "url": world_hub_public_url(host=host, port=port),
        "enabled": bool(cfg.get("enabled", True)),
        "worlds": worlds,
        "world_count": len(worlds),
    }

    if getattr(args, "json_output", False):
        print(json.dumps({"world": row}, indent=2, sort_keys=True))
        return 0

    pid_text = f" pid={row['pid']}" if row["pid"] is not None else ""
    print(f"world: {row['status']}{pid_text}")
    print(f"  url: {row['url']}")
    print(f"  pid: {row['pid_path']}")
    print(f"  log: {row['log_path']}")
    if worlds:
        print("  worlds:")
        for item in worlds:
            present = item.get("present_count")
            extra = f" present={present}" if present is not None else ""
            print(f"    {item.get('id')} ({item.get('name')}){extra}")
    else:
        print("  worlds: (none)")
    return 0


def handle_world_logs(args: argparse.Namespace) -> int:
    paths = world_hub_paths()
    lines = max(1, int(getattr(args, "lines", 80)))
    print(f"==> world ({paths.log_path})")
    if getattr(args, "follow", False):
        from .runtime import _follow_log

        _follow_log(paths.log_path)
        return 0
    text = tail_text(paths.log_path, max_lines=lines)
    print(text or "(no log output)")
    return 0


def handle_world_open(args: argparse.Namespace) -> int:
    import webbrowser

    cfg = world_hub_config()
    if not cfg.get("enabled", True):
        print("World hub is disabled in config (world.enabled=false).")
        return 1

    paths = world_hub_paths()
    if running_pid(paths.pid_path) is None:
        print("World hub is not running. Start it with: xagent world start")
        return 1

    host, port = _resolved_hub_bind(args)
    url = world_hub_public_url(host=host, port=port)
    if webbrowser.open(url):
        print(f"Opened: {url}")
        return 0
    print(f"Failed to open: {url}")
    return 1


def handle_run_world_internal(args: argparse.Namespace) -> int:
    from agents_world.server import WorldHub

    host, port = _resolved_hub_bind(args)
    data_root = str(getattr(args, "data_root", None) or world_hub_runtime_root())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    hub = WorldHub.create(host=host, port=port, data_root=data_root)

    async def _run() -> None:
        await hub.start()
        if getattr(args, "open_browser", False):
            import webbrowser

            webbrowser.open(world_hub_public_url(host=hub.host, port=hub.port))
        try:
            if hub._server is None:
                return
            await hub._server.serve_forever()
        finally:
            await hub.stop()

    try:
        asyncio.run(_run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 0
    return 0


def handle_world_list(args: argparse.Namespace) -> int:
    host, port = _resolved_hub_bind(args)
    worlds = list_world_summaries(host=host, port=port)
    if getattr(args, "json_output", False):
        print(json.dumps({"worlds": worlds, "hub_running": world_hub_is_running()}, indent=2, sort_keys=True))
        return 0
    if not worlds:
        print("No worlds yet. Create one with: xagent world create plaza")
        return 0
    for item in worlds:
        present = item.get("present_count")
        extra = f"  present={present}" if present is not None else ""
        print(f"{item.get('id')}\t{item.get('name')}{extra}")
    return 0


def handle_world_create(args: argparse.Namespace) -> int:
    name = str(getattr(args, "name", "") or "").strip()
    if not name:
        print("Error: world name is required")
        return 1
    host, port = _resolved_hub_bind(args)
    if world_hub_is_running() and wait_for_world_hub(host=host, port=port, timeout=1.0):
        status, payload, error = create_world_on_hub(name, host=host, port=port)
        if status == 201:
            print(json.dumps({"id": payload.get("id"), "name": payload.get("name")}, ensure_ascii=False))
            return 0
        message = payload.get("error") or error or f"HTTP {status}"
        print(f"Error: {message}")
        return 1
    code, payload, error = create_world_on_disk(name)
    if code != 0:
        print(f"Error: {error or 'failed to create world'}")
        return code
    print(json.dumps({"id": payload.get("id"), "name": payload.get("name")}, ensure_ascii=False))
    return 0


def handle_world_remove(args: argparse.Namespace) -> int:
    from agents_world.paths import validate_world_id, world_data_dir

    world_id = str(getattr(args, "world", "") or getattr(args, "world_id", "") or "").strip()
    if not world_id:
        print("Error: world id is required")
        return 1
    try:
        world_id = validate_world_id(world_id)
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1

    host, port = _resolved_hub_bind(args)
    known = {str(item.get("id") or "") for item in list_world_summaries(host=host, port=port)}
    data_root = world_hub_runtime_root()
    data_dir = world_data_dir(world_id, root=data_root)
    if world_id not in known and not data_dir.exists():
        print(f"Error: unknown world {world_id!r}. List worlds with: xagent world list")
        return 1

    if not _confirm_world_delete(world_id, data_dir, assume_yes=bool(getattr(args, "yes", False))):
        print("Remove cancelled.")
        return 1

    if world_hub_is_running() and wait_for_world_hub(host=host, port=port, timeout=1.0):
        status, payload, error = delete_world_on_hub(world_id, host=host, port=port)
        if status == 200:
            cleared = clear_local_presence_for_world(world_id)
            print(json.dumps({"id": payload.get("id") or world_id, "deleted": True}, ensure_ascii=False))
            for agent_name in cleared:
                print(f"{agent_name}: recorded leave from {world_id}")
            return 0
        if status != 404:
            message = payload.get("error") or error or f"HTTP {status}"
            print(f"Error: {message}")
            return 1

    code, payload, error = delete_world_on_disk(world_id)
    if code != 0:
        print(f"Error: {error or 'failed to delete world'}")
        return code
    cleared = clear_local_presence_for_world(world_id)
    print(json.dumps({"id": payload.get("id") or world_id, "deleted": True}, ensure_ascii=False))
    for agent_name in cleared:
        print(f"{agent_name}: recorded leave from {world_id}")
    return 0


def _ensure_hub_running(args: argparse.Namespace) -> int:
    if world_hub_is_running() and wait_for_world_hub(timeout=1.0):
        return 0
    if not getattr(args, "start_hub", False):
        print("World hub is not running. Start it with: xagent world start")
        print("Or pass --start-hub to start it for this command.")
        return 1
    started, already_running = _start_background_world(args)
    if not started:
        return 1
    if not already_running:
        _after_hub_up(args, restore=True)
    elif not wait_for_world_hub(timeout=HUB_READY_TIMEOUT):
        print("World hub did not become reachable. Check: xagent world logs")
        return 1
    return 0


def _ensure_api_running(args: argparse.Namespace, config_dir: Path) -> int:
    paths = managed_paths(config_dir, CHANNEL_API)
    if running_pid(paths.pid_path) is not None and wait_for_agent_world_api(config_dir, timeout=1.0):
        return 0
    if not getattr(args, "start_api", False):
        print("This agent's API channel is not running (needed to enter a world).")
        print("Start it with: xagent api start")
        print("Or pass --start-api to start it for this command.")
        return 1
    from .runtime import _start_background_channel

    if running_pid(paths.pid_path) is None:
        if not _start_background_channel(args, channel=CHANNEL_API, config_dir=config_dir):
            return 1
    if not wait_for_agent_world_api(config_dir):
        print("API channel started but /world/status is not reachable.")
        return 1
    return 0


def _resolve_join_agent(args: argparse.Namespace) -> tuple[Optional[str], Optional[Path], int]:
    try:
        config_dir = runtime_dir(args)
    except AgentRegistryError as exc:
        print(f"Error: {exc}")
        return None, None, 1
    explicit = getattr(args, "agent", None)
    if explicit:
        try:
            agent_name = resolve_agent_name(explicit)
        except AgentRegistryError as exc:
            print(f"Error: {exc}")
            return None, None, 1
        return agent_name, config_dir, 0
    if getattr(args, "config_dir", None):
        return Path(config_dir).name, config_dir, 0
    try:
        return resolve_agent_name(None), config_dir, 0
    except AgentRegistryError:
        return Path(config_dir).name, config_dir, 0


def handle_world_join(args: argparse.Namespace) -> int:
    world_id = str(getattr(args, "world", "") or getattr(args, "world_id", "") or "").strip()
    if not world_id:
        print("Error: world id is required")
        return 1
    hub_code = _ensure_hub_running(args)
    if hub_code != 0:
        return hub_code
    agent_name, config_dir, code = _resolve_join_agent(args)
    if code != 0 or config_dir is None or agent_name is None:
        return code
    api_code = _ensure_api_running(args, config_dir)
    if api_code != 0:
        return api_code

    host, port = _resolved_hub_bind(args)
    known = {str(item.get("id") or "") for item in list_world_summaries(host=host, port=port)}
    if world_id not in known:
        print(f"Error: unknown world {world_id!r}. Create it with: xagent world create {world_id}")
        return 1

    member_id = str(getattr(args, "member_id", None) or agent_name).strip() or agent_name
    display_name = str(getattr(args, "name", None) or member_id).strip() or member_id
    world_url = world_ws_url(world_id, host=host, port=port)
    status, payload, error = _http_json(
        "POST",
        agent_api_public_url(config_dir).rstrip("/") + "/world/join",
        body={"world_url": world_url, "member_id": member_id, "display_name": display_name},
        timeout=5.0,
    )
    if not (200 <= status < 300):
        detail = payload.get("detail") or payload.get("error") or error or f"HTTP {status}"
        print(f"Error: failed to join: {detail}")
        return 1
    mark_world_presence(
        config_dir,
        world_url=world_url,
        member_id=member_id,
        display_name=display_name,
        world_id=str(payload.get("world_id") or world_id),
        want_present=True,
    )
    print(f"{agent_name} joined {payload.get('world_id') or world_id} as {display_name}({member_id})")
    return 0


def handle_world_leave(args: argparse.Namespace) -> int:
    agent_name, config_dir, code = _resolve_join_agent(args)
    if code != 0 or config_dir is None or agent_name is None:
        return code
    if running_pid(managed_paths(config_dir, CHANNEL_API).pid_path) is None:
        presence = mark_world_left(config_dir)
        world_id = (presence or {}).get("world_id") or "world"
        print(f"{agent_name}: api channel is not running; recorded leave from {world_id}")
        return 0
    status, payload, error = _http_json(
        "POST",
        agent_api_public_url(config_dir).rstrip("/") + "/world/leave",
        timeout=5.0,
    )
    mark_world_left(config_dir)
    if not (200 <= status < 300) and status != 0:
        detail = payload.get("detail") or payload.get("error") or error or f"HTTP {status}"
        print(f"Error: failed to leave: {detail}")
        return 1
    if status == 0:
        print(f"{agent_name}: api channel is not reachable; recorded leave locally")
        return 0
    print(f"{agent_name} left the world")
    return 0


def handle_world_chat(args: argparse.Namespace) -> int:
    world_id = str(getattr(args, "world", "") or getattr(args, "world_id", "") or "").strip()
    if not world_id:
        print("Error: world id is required")
        return 1
    hub_code = _ensure_hub_running(args)
    if hub_code != 0:
        return hub_code
    host, port = _resolved_hub_bind(args)
    member_id = str(getattr(args, "member_id", None) or "human").strip() or "human"
    display_name = str(getattr(args, "name", None) or member_id).strip() or member_id
    from agents_world.cli import main as agents_world_main

    return agents_world_main(
        [
            "join",
            "--world-id",
            world_id,
            "--host",
            world_hub_browse_host(host),
            "--port",
            str(port),
            "--member-id",
            member_id,
            "--name",
            display_name,
        ]
    )
