"""Runtime command handlers for the CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from ..base import BaseAgentConfig
from .agents import AgentRegistryError, management_root, resolve_agent_name
from .channels import (
    CHANNEL_API,
    CHANNEL_VOICE,
    CHANNEL_WEIXIN,
    ChannelSelectionError,
    enabled_channels_from_config,
    normalize_channel_values,
    voice_config,
    weixin_config,
)
from .chat import AgentCLI
from .paths import (
    config_path,
    identity_path,
    load_client_runtime_config,
    load_runtime_config,
    runtime_dir,
    runtime_dir_or_management_root,
)
from .processes import (
    running_pid,
    start_background,
    stop_managed_process,
    tail_text,
)
from .web_client import (
    web_client_config,
    web_client_paths,
    web_client_public_url,
)


def handle_chat(args: argparse.Namespace) -> int:
    agent_cli = AgentCLI(config_dir=str(runtime_dir(args)), verbose=args.verbose)

    if args.message is None:
        async def run_interactive_chat():
            await agent_cli.chat_interactive(
                user_id=args.user_id,
                stream=args.stream,
            )

        asyncio.run(run_interactive_chat())
        return 0

    event_mode = bool(args.events or args.stream is not None)
    stream = bool(args.stream) if args.stream is not None else False

    async def run_single_message():
        if event_mode:
            await agent_cli.print_single_chat_events(
                message=args.message,
                user_id=args.user_id,
                stream=stream,
            )
            return

        response = await agent_cli.chat_single(
            message=args.message,
            user_id=args.user_id,
        )
        print(response)

    asyncio.run(run_single_message())
    return 0


def handle_voice(args: argparse.Namespace) -> int:
    print("Host audio capture has been removed. Configure voice with xagent voice setup, "
          "then use the Voice page or a paired WebSocket voice device.")
    return 1 if getattr(args, "list_devices", False) else 0


def _handle_channel_error(exc: ChannelSelectionError) -> int:
    print(f"Error: {exc}")
    return 1


def _web_spawn_target(args: argparse.Namespace) -> tuple[Optional[str], Optional[str]]:
    """Return ``(config_dir, agent_name)`` for a managed web client subprocess."""
    config_dir = getattr(args, "config_dir", None)
    if config_dir:
        return str(config_dir), None
    try:
        return None, resolve_agent_name(getattr(args, "agent", None))
    except AgentRegistryError:
        return str(management_root()), None


def _web_command(args: argparse.Namespace) -> list[str]:
    command = [sys.executable, "-m", "xagent.interfaces.cli", "_run-web"]
    config_dir, agent_name = _web_spawn_target(args)
    if config_dir:
        command.extend(["--config-dir", config_dir])
    elif agent_name:
        command.extend(["--agent", agent_name])

    for flag, attr in (("--host", "host"), ("--port", "port"), ("--api-url", "api_url")):
        value = getattr(args, attr, None)
        if value is not None:
            command.extend([flag, str(value)])
    if getattr(args, "open_browser", False):
        command.append("--open")
    return command


def _web_client_runtime_values(
    args: argparse.Namespace,
    config: dict[str, Any],
) -> tuple[str, int, str, bool, str, Optional[str]]:
    web_cfg = web_client_config(config)
    host = getattr(args, "host", None) or web_cfg.get("host")
    port = getattr(args, "port", None)
    if port is None:
        port = web_cfg.get("port")
    api_url = str(getattr(args, "api_url", None) or web_cfg.get("api_url") or "").strip()
    open_browser = bool(getattr(args, "open_browser", False))

    raw_config_dir = getattr(args, "config_dir", None)
    config_dir = raw_config_dir or str(runtime_dir_or_management_root(args))
    initial_agent: Optional[str] = None
    if not raw_config_dir:
        try:
            initial_agent = resolve_agent_name(getattr(args, "agent", None))
        except AgentRegistryError:
            initial_agent = None
    return str(host), int(port), api_url, open_browser, config_dir, initial_agent


def _run_web_client(args: argparse.Namespace, config: dict[str, Any]) -> int:
    from ..web import WebClientServer

    host, port, api_url, open_browser, config_dir, initial_agent = _web_client_runtime_values(args, config)
    server = WebClientServer(
        host=host,
        port=port,
        api_url=api_url,
        config_dir=config_dir,
        initial_agent=initial_agent,
    )
    print(f"xAgent web client ready at http://{host}:{port} (api={api_url}).")
    server.run(open_browser=open_browser)
    return 0


def handle_run_web_internal(args: argparse.Namespace) -> int:
    try:
        config = load_client_runtime_config(args)
    except ChannelSelectionError as exc:
        return _handle_channel_error(exc)
    return _run_web_client(args, config)


def handle_run(args: argparse.Namespace) -> int:
    from .agent_runtime import handle_run as handler
    return handler(args)


def handle_start(args: argparse.Namespace) -> int:
    from .agent_runtime import handle_start as handler
    return handler(args)

def handle_stop(args: argparse.Namespace) -> int:
    from .agent_runtime import handle_stop as handler
    return handler(args)

def handle_restart(args: argparse.Namespace) -> int:
    from .agent_runtime import handle_restart as handler
    return handler(args)

def handle_status(args: argparse.Namespace) -> int:
    from .agent_runtime import handle_status as handler
    return handler(args)

def _start_background_web(args: argparse.Namespace) -> tuple[bool, bool]:
    """Start the web client. Returns (success, already_running)."""
    paths = web_client_paths()
    config = load_client_runtime_config(args)
    host, port, *_ = _web_client_runtime_values(args, config)
    browse_host = "127.0.0.1" if host == "0.0.0.0" else "::1" if host == "::" else host
    if ":" in browse_host:
        browse_host = f"[{browse_host}]"
    readiness_url = f"http://{browse_host}:{port}/api/ready"
    from urllib.request import ProxyHandler, build_opener
    opener = build_opener(ProxyHandler({}))
    def ready(pid: int) -> bool:
        try:
            with opener.open(readiness_url, timeout=0.2) as response:
                result = json.load(response)
                return bool(result.get("web")) and result.get("pid") == pid
        except (OSError, ValueError):
            return False
    result = start_background(
        _web_command(args),
        pid_path=paths.pid_path,
        log_path=paths.log_path,
        startup_timeout=10.0,
        ready_probe=ready,
    )
    if result.ok:
        print(f"Started web client in background (pid={result.pid}).")
        print(f"Logs: {paths.log_path}")
        return True, False

    if result.error.startswith("already running"):
        print(f"Web client is already running (pid={result.pid}).")
        return True, True

    print(f"Failed to start web client: {result.error}")
    if result.recent_output:
        print(result.recent_output)
    return False, False


def handle_web_start(args: argparse.Namespace) -> int:
    try:
        load_client_runtime_config(args)
    except ChannelSelectionError as exc:
        return _handle_channel_error(exc)

    started, already_running = _start_background_web(args)
    if not started:
        return 1
    if getattr(args, "open_browser", False) and already_running:
        return handle_web_open(args)
    return 0


def handle_web_stop(args: argparse.Namespace) -> int:
    paths = web_client_paths()
    stopped, message = stop_managed_process(paths.pid_path)
    print(f"web: {message}")
    return 0 if stopped else 1


def handle_web_restart(args: argparse.Namespace) -> int:
    paths = web_client_paths()
    stopped, message = stop_managed_process(paths.pid_path)
    print(f"web: {message}")
    if not stopped:
        return 1
    started, _already_running = _start_background_web(args)
    return 0 if started else 1


def handle_web_status(args: argparse.Namespace) -> int:
    try:
        config = load_client_runtime_config(args)
    except ChannelSelectionError as exc:
        return _handle_channel_error(exc)

    paths = web_client_paths()
    pid = running_pid(paths.pid_path)
    row = {
        "status": "running" if pid is not None else "stopped",
        "pid": pid,
        "pid_path": str(paths.pid_path),
        "log_path": str(paths.log_path),
        "url": web_client_public_url(config),
    }

    if getattr(args, "json_output", False):
        print(json.dumps({"web": row}, indent=2, sort_keys=True))
        return 0

    pid_text = f" pid={row['pid']}" if row["pid"] is not None else ""
    print(f"web: {row['status']}{pid_text}")
    print(f"  url: {row['url']}")
    print(f"  pid: {row['pid_path']}")
    print(f"  log: {row['log_path']}")
    return 0


def handle_web_logs(args: argparse.Namespace) -> int:
    paths = web_client_paths()
    lines = max(1, int(getattr(args, "lines", 80)))
    print(f"==> web ({paths.log_path})")
    if getattr(args, "follow", False):
        _follow_log(paths.log_path)
        return 0
    text = tail_text(paths.log_path, max_lines=lines)
    print(text or "(no log output)")
    return 0


def handle_web_open(args: argparse.Namespace) -> int:
    import webbrowser

    try:
        config = load_client_runtime_config(args)
    except ChannelSelectionError as exc:
        return _handle_channel_error(exc)

    web_cfg = web_client_config(config)
    if not web_cfg.get("enabled", True):
        print("Web client is disabled in config (web.enabled=false).")
        return 1

    paths = web_client_paths()
    if running_pid(paths.pid_path) is None:
        print("Web client is not running. Start it with: xagent web start")
        return 1

    url = web_client_public_url(config)
    if webbrowser.open(url):
        print(f"Opened: {url}")
        return 0
    print(f"Failed to open: {url}")
    return 1


def _follow_log(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8", errors="replace") as handle:
        handle.seek(0, os.SEEK_END)
        while True:
            line = handle.readline()
            if line:
                print(line, end="")
                continue
            time.sleep(0.2)


def handle_logs(args: argparse.Namespace) -> int:
    from .agent_runtime import handle_logs as handler
    return handler(args)

def handle_observe(args: argparse.Namespace) -> int:
    from .agent_runtime import ensure_runtime
    try:
        metadata = json.loads(args.metadata) if args.metadata else None
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("--metadata must be a JSON object")
        async def submit():
            client = await ensure_runtime(runtime_dir(args))
            return await client.observe(context=args.text, source=args.source, event_type=args.event_type, metadata=metadata)
        print(json.dumps(asyncio.run(submit()), ensure_ascii=False, indent=2, default=str))
        return 0
    except Exception as exc:
        print(f"Cannot observe: {exc}")
        return 1

def handle_config(args: argparse.Namespace) -> int:
    path = config_path(args)
    if args.config_command == "path":
        print(path)
        return 0
    if args.config_command == "show":
        if not path.is_file():
            print(f"Config not found: {path}")
            return 1
        print(path.read_text(encoding="utf-8"), end="")
        return 0
    if args.config_command == "validate":
        from .config_editor import load_config, validate_config
        validate_config(load_config(runtime_dir(args)))
        print(f"Config OK: {path}")
        return 0
    print(f"Unknown config command: {args.config_command}")
    return 1


def handle_identity(args: argparse.Namespace) -> int:
    path = identity_path(args)
    if args.identity_command == "path":
        print(path)
        return 0
    if args.identity_command == "show":
        if not path.is_file():
            print(f"Identity not found: {path}")
            return 1
        print(path.read_text(encoding="utf-8"), end="")
        return 0
    print(f"Unknown identity command: {args.identity_command}")
    return 1


def _memory_root(args: argparse.Namespace) -> Path:
    return runtime_dir(args) / BaseAgentConfig.MEMORY_DIRNAME


def _memory_scope_root(args: argparse.Namespace) -> Path:
    scope = getattr(args, "scope", "all")
    root = _memory_root(args)
    return root if scope == "all" else root / scope


def handle_memory(args: argparse.Namespace) -> int:
    root = _memory_root(args)
    scope_root = _memory_scope_root(args)

    if args.memory_command == "stats":
        files = sorted(scope_root.rglob("*.md")) if scope_root.exists() else []
        total_bytes = sum(path.stat().st_size for path in files if path.is_file())
        print(f"Memory root: {root}")
        print(f"Scope: {getattr(args, 'scope', 'all')}")
        print(f"Files: {len(files)}")
        print(f"Bytes: {total_bytes}")
        return 0

    if args.memory_command == "list":
        from ...components.memory import MarkdownMemory

        days = int(getattr(args, "days", 7) or 7)
        if days <= 0:
            print("--days must be a positive whole number")
            return 1

        async def _run_memory_list() -> int:
            memory = MarkdownMemory(str(root))
            entries = await memory.read_recent_dailies(days=days)
            if not entries:
                unit = "day" if days == 1 else "days"
                print(f"No daily journals found in the last {days} {unit}.")
                return 0
            for index, (date_label, text) in enumerate(entries):
                if index:
                    print("\n---\n")
                print(f"# {date_label}\n")
                print(text.strip())
            return 0

        return asyncio.run(_run_memory_list())

    if args.memory_command == "search":
        if not scope_root.exists():
            return 0
        needle = args.query.casefold()
        for path in sorted(scope_root.rglob("*.md")):
            if not path.is_file():
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            for line_number, line in enumerate(lines, 1):
                if needle in line.casefold():
                    print(f"{path.relative_to(root)}:{line_number}:{line}")
        return 0

    if args.memory_command == "clear":
        import shutil
        from ...core.runtime.ownership import RuntimeOwnership, runtime_is_active
        from ...core.runtime.client import RuntimeClient

        if not getattr(args, "yes", False):
            print("Refusing to clear memory without --yes")
            return 1
        if runtime_is_active(runtime_dir(args)):
            async def clear():
                await RuntimeClient(runtime_dir(args)).request("POST", "/api/memory/clear",
                    params={"scope": getattr(args, "scope", "all")})
            asyncio.run(clear())
        else:
            with RuntimeOwnership(runtime_dir(args)):
                target = scope_root
                if target.exists():
                    shutil.rmtree(target)
                target.mkdir(parents=True, exist_ok=True)
        print(f"Cleared memory scope: {getattr(args, 'scope', 'all')}")
        return 0

    print(f"Unknown memory command: {args.memory_command}")
    return 1


def handle_messages(args: argparse.Namespace) -> int:
    from ..server.admin_service import AdminService
    runner = AdminService(config_dir=str(runtime_dir(args)))
    storage = runner.message_storage

    async def _run_messages() -> int:
        if args.messages_command == "stats":
            total = await storage.get_message_count()
            info = storage.get_stream_info() if hasattr(storage, "get_stream_info") else {}
            print(json.dumps({"total": total, "storage": info}, indent=2, sort_keys=True))
            return 0

        if args.messages_command == "list":
            messages = await storage.get_messages(count=args.count, offset=args.offset)
            payload = []
            for message in messages:
                item = message.model_dump(mode="json") if hasattr(message, "model_dump") else str(message)
                payload.append(item)
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            return 0

        if args.messages_command == "clear":
            if not getattr(args, "yes", False):
                print("Refusing to clear messages without --yes")
                return 1
            from ...core.runtime.ownership import RuntimeOwnership, runtime_is_active
            from ...core.runtime.client import RuntimeClient
            if runtime_is_active(runtime_dir(args)):
                await RuntimeClient(runtime_dir(args)).request("POST", "/clear_messages")
            else:
                with RuntimeOwnership(runtime_dir(args)):
                    await storage.clear_messages()
            print("Cleared message stream")
            return 0

        print(f"Unknown messages command: {args.messages_command}")
        return 1

    return asyncio.run(_run_messages())


def handle_doctor(args: argparse.Namespace) -> int:
    config_dir = runtime_dir(args)
    config_file = config_dir / BaseAgentConfig.CONFIG_FILENAME
    identity_file = config_dir / BaseAgentConfig.IDENTITY_FILENAME
    ok = True

    print(f"Runtime dir: {config_dir}")
    if config_file.is_file():
        print(f"Config: ok ({config_file})")
        mode = config_file.stat().st_mode & 0o777
        if mode & 0o077:
            print(f"Config permissions: too open ({oct(mode)}); expected 600")
            ok = False
    else:
        print(f"Config: missing ({config_file})")
        ok = False

    if identity_file.is_file() and identity_file.read_text(encoding="utf-8").strip():
        print(f"Identity: ok ({identity_file})")
    else:
        print(f"Identity: missing or empty ({identity_file})")
        ok = False

    try:
        config = load_runtime_config(args)
        raw_channels = getattr(args, "channels", None)
        channels = (
            normalize_channel_values(raw_channels, default=CHANNEL_API, config=config)
            if raw_channels
            else enabled_channels_from_config(config)
        )
    except ChannelSelectionError as exc:
        print(f"Channels: {exc}")
        return 1

    print(f"Channels: {', '.join(channels)}")
    if CHANNEL_WEIXIN in channels:
        data = weixin_config(config)
        if data.get("account_id"):
            print("Weixin: configured")
        else:
            print("Weixin: missing account_id")
            ok = False
    if CHANNEL_VOICE in channels:
        try:
            from ..voice.config import VoiceChannelConfig

            data = voice_config(config)
            if not data:
                raise ValueError("channels.voice is not configured")
            voice_runtime_config = VoiceChannelConfig.from_dict(data)
            voice_runtime_config.resolved_api_key()
            print("Voice: configured")
        except Exception as exc:
            print(f"Voice: {exc}")
            ok = False
    if args.online:
        online_ok, online_message = _provider_online_check(config)
        print(online_message)
        if not online_ok:
            ok = False
    return 0 if ok else 1


def _provider_online_check(config: dict[str, Any]) -> tuple[bool, str]:
    """Ping the configured provider without writing a conversation."""
    from ...core.providers import provider_base_url
    from ...tools.search_tool import is_placeholder_api_key

    provider_cfg = config.get("provider") if isinstance(config.get("provider"), dict) else {}
    api_key = str(provider_cfg.get("api_key") or "").strip()
    if is_placeholder_api_key(api_key):
        return False, "Provider: missing or placeholder api_key"

    base_url = str(provider_cfg.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        base_url = provider_base_url(str(provider_cfg.get("name") or "")).rstrip("/")
    url = f"{base_url}/models"
    try:
        import httpx

        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )
    except Exception as exc:
        return False, f"Provider: could not reach {url} ({exc})"
    if response.status_code in (401, 403):
        return False, "Provider: API key rejected"
    if 200 <= response.status_code < 300:
        return True, "Provider: ok"
    return True, f"Provider: reachable (HTTP {response.status_code} from /models)"


def handle_version(_args: argparse.Namespace) -> int:
    try:
        from xagent.__version__ import __version__
    except Exception:  # pragma: no cover - defensive
        __version__ = "unknown"
    print(f"xAgent {__version__}")
    print(f"Python {sys.version.split()[0]}")
    return 0


def _runtime_is_initialized(config_dir: Path) -> bool:
    config_file = config_dir / BaseAgentConfig.CONFIG_FILENAME
    identity_file = config_dir / BaseAgentConfig.IDENTITY_FILENAME
    if not config_file.is_file() or not identity_file.is_file():
        return False
    try:
        return bool(identity_file.read_text(encoding="utf-8").strip())
    except OSError:
        return False


def print_quick_start() -> None:
    try:
        config_dir = runtime_dir(_launcher_args(agent=None, config_dir=None))
        initialized = _runtime_is_initialized(config_dir)
        runtime_label = str(config_dir)
    except Exception:
        initialized = False
        runtime_label = "(no active agent)"

    print("xAgent")
    print(f"Runtime: {runtime_label}")
    print("")
    if not initialized:
        print("First time? Run:  xagent agents create default")
        print("")
    print("Use now:")
    print("  xagent chat                     Chat in the terminal")
    print("  xagent web open                 Open the browser web client")
    print("  xagent voice                    Use microphone / speaker mode")
    print("")
    print("Keep running:")
    print("  xagent api start                Start the api channel")
    print("  xagent web start                Start the browser web client")
    print("  xagent voice start              Start voice channel")
    print("  xagent status                   Show channel and client status")
    print("  xagent logs -f              Follow the Agent runtime log")
    print("  xagent web logs -f              Follow web client logs")
    print("")
    print("Setup and inspect:")
    print("  xagent setup                    Reconfigure the active agent")
    print("  xagent voice setup              Configure the voice channel")
    print("  xagent agents list              List agents")
    print("  xagent agents select work       Switch active agent")
    print("  xagent config validate          Validate config.yaml")
    print("  xagent memory list --days 7     Show recent daily journals")
    print("  xagent doctor                   Check readiness")
    print("")
    print("Full help: xagent --help")


def _xagent_version_text() -> str:
    try:
        from xagent.__version__ import __version__
    except Exception:  # pragma: no cover - defensive
        return "unknown"
    return __version__


def _launcher_args(**kwargs: Any) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)
