"""Runtime command handlers for the CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from ...core.runtime import create_runtime_heartbeat
from ..base import BaseAgentConfig, BaseAgentRunner
from .channels import (
    CHANNEL_API,
    CHANNEL_FEISHU,
    CHANNEL_WEIXIN,
    ChannelSelectionError,
    api_config,
    default_start_channel_from_config,
    enabled_channels_from_config,
    feishu_config,
    normalize_channel_values,
    voice_config,
    weixin_config,
)
from .chat import AgentCLI
from .paths import config_path, identity_path, load_runtime_config, runtime_dir
from .processes import managed_paths, running_pid, start_background, stop_managed_process, tail_text


def handle_chat(args: argparse.Namespace) -> int:
    agent_cli = AgentCLI(config_dir=args.config_dir, verbose=args.verbose)

    if args.message is None:
        async def run_interactive_chat():
            await agent_cli.chat_interactive(
                user_id=args.user_id,
                stream=args.stream,
            )

        asyncio.run(run_interactive_chat())
        return 0

    event_mode = bool(args.events or args.stream is not None or hasattr(agent_cli.agent, "chat_events"))
    stream = bool(args.stream) if args.stream is not None else False

    async def run_single_message():
        if event_mode and hasattr(agent_cli.agent, "chat_events"):
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
    if getattr(args, "verbose", False):
        logging.getLogger().setLevel(logging.INFO)
        logging.getLogger("xagent").setLevel(logging.INFO)
    else:
        logging.getLogger().setLevel(logging.CRITICAL)
        logging.getLogger("xagent").setLevel(logging.CRITICAL)

    try:
        if getattr(args, "list_devices", False):
            from ...voice.audio import list_audio_devices_text

            print(list_audio_devices_text())
            return 0

        runner = BaseAgentRunner(config_dir=args.config_dir)
        from ...voice.config import VoiceChannelConfig
        from ...voice.factory import create_local_voice_runtime
        from ...voice.runtime import VoiceRuntimeOptions

        runtime_config = VoiceChannelConfig.from_dict(voice_config(runner.config))
        runtime = create_local_voice_runtime(
            agent=runner.agent,
            config=runtime_config,
            options=VoiceRuntimeOptions(
                user_id=args.user_id or "local_voice",
                stream=True,
                tasks_dir=getattr(runner, "tasks_dir", None),
            ),
            input_device=getattr(args, "input_device", None),
            output_device=getattr(args, "output_device", None),
        )
    except Exception as exc:
        print(f"Failed to start voice channel: {exc}")
        return 1

    try:
        asyncio.run(runtime.run_forever())
    except KeyboardInterrupt:
        print("\nVoice channel stopped.")
    except Exception as exc:
        print(f"Voice channel error: {exc}")
        return 1
    return 0


def handle_server(args: argparse.Namespace) -> int:
    from ..server import AgentHTTPServer

    server_kwargs = {
        "config_dir": args.config_dir,
        "enable_web": not args.no_web,
    }
    if args.max_concurrent_chats is not None:
        server_kwargs["max_concurrent_chats"] = args.max_concurrent_chats
    if args.queue_timeout is not None:
        server_kwargs["chat_queue_timeout"] = args.queue_timeout
    if args.chat_timeout is not None:
        server_kwargs["chat_timeout"] = args.chat_timeout

    server = AgentHTTPServer(**server_kwargs)
    server.run(host=args.host, port=args.port, open_browser=args.open_browser)
    return 0


def handle_web(args: argparse.Namespace) -> int:
    try:
        config = load_runtime_config(args)
    except ChannelSelectionError as exc:
        return _handle_channel_error(exc)
    return _run_api_channel(args, config)


def _channel_arg_values(args: argparse.Namespace) -> Optional[list[str]]:
    values = getattr(args, "channels", None)
    if values is None:
        return None
    if isinstance(values, str):
        return [values]
    return list(values)


def _select_channels(args: argparse.Namespace, *, default: str) -> tuple[list[str], dict[str, Any]]:
    config = load_runtime_config(args)
    values = _channel_arg_values(args)
    if values is None and default == "auto":
        channels = [default_start_channel_from_config(config)]
    else:
        channels = normalize_channel_values(values, default=default, config=config)
    return channels, config


def _handle_channel_error(exc: ChannelSelectionError) -> int:
    print(f"Error: {exc}")
    return 1


def _channel_command(channel: str, args: argparse.Namespace) -> list[str]:
    command = [sys.executable, "-m", "xagent.interfaces.cli", "_run-channel", channel]
    config_dir = getattr(args, "config_dir", None)
    if config_dir:
        command.extend(["--dir", config_dir])

    for flag, attr in (
        ("--host", "host"),
        ("--port", "port"),
        ("--max-concurrent-chats", "max_concurrent_chats"),
        ("--queue-timeout", "queue_timeout"),
        ("--chat-timeout", "chat_timeout"),
    ):
        value = getattr(args, attr, None)
        if value is not None:
            command.extend([flag, str(value)])
    if getattr(args, "open_browser", False):
        command.append("--open")
    return command


def _api_runtime_values(
    args: argparse.Namespace,
    config: dict[str, Any],
) -> tuple[dict[str, Any], Optional[str], Optional[int], bool]:
    api_cfg = api_config(config)
    server_kwargs: dict[str, Any] = {
        "config_dir": getattr(args, "config_dir", None),
    }

    runtime_mapping = (
        ("max_concurrent_chats", "max_concurrent_chats"),
        ("queue_timeout", "chat_queue_timeout"),
        ("chat_timeout", "chat_timeout"),
    )
    for args_attr, server_key in runtime_mapping:
        value = getattr(args, args_attr, None)
        if value is None:
            value = api_cfg.get(args_attr)
        if value is not None:
            server_kwargs[server_key] = value

    host = getattr(args, "host", None) or api_cfg.get("host")
    port = getattr(args, "port", None)
    if port is None:
        port = api_cfg.get("port")
    open_browser = bool(getattr(args, "open_browser", False))
    return server_kwargs, host, port, open_browser


def _run_api_channel(args: argparse.Namespace, config: dict[str, Any]) -> int:
    from ..server import AgentHTTPServer

    server_kwargs, host, port, open_browser = _api_runtime_values(args, config)
    server = AgentHTTPServer(**server_kwargs)
    print(f"xAgent api channel ready (model={server.agent.model}).")
    server.run(host=host, port=port, open_browser=open_browser)
    return 0


def _run_feishu_channel(args: argparse.Namespace, config: dict[str, Any]) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        from ...integrations.feishu import FeishuAdapter, FeishuAdapterConfig
    except ImportError as exc:  # pragma: no cover - defensive
        print(f"Failed to import Feishu adapter: {exc}")
        return 1

    feishu_data = feishu_config(config)
    if not feishu_data:
        print("Feishu channel is not configured. Run: xagent channel feishu setup")
        return 1

    try:
        feishu_runtime_config = FeishuAdapterConfig.from_dict(feishu_data)
    except Exception as exc:
        print(f"Invalid Feishu channel config: {exc}")
        return 1

    runner = BaseAgentRunner(config_dir=getattr(args, "config_dir", None))
    adapter = FeishuAdapter(agent=runner.agent, config=feishu_runtime_config)

    async def _run_daemon() -> bool:
        heartbeat = create_runtime_heartbeat(
            runner.agent,
            config.get("runtime") if isinstance(config, dict) else None,
            logger_=logging.getLogger(__name__),
        )
        stop_requested = False
        loop = asyncio.get_running_loop()
        old_handlers: dict[int, object] = {}
        signal_handlers: list[int] = []

        def _request_stop() -> None:
            nonlocal stop_requested
            stop_requested = True
            adapter._stop_event.set()
            adapter._safe_stop()

        def _handle_stop(_signum: int, _frame) -> None:
            loop.call_soon_threadsafe(_request_stop)

        for signum in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
            if signum is None:
                continue
            try:
                loop.add_signal_handler(signum, _request_stop)
                signal_handlers.append(signum)
            except (NotImplementedError, RuntimeError):
                old_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, _handle_stop)

        try:
            if heartbeat is not None:
                await heartbeat.start()
            await adapter.run()
        finally:
            for signum in signal_handlers:
                try:
                    loop.remove_signal_handler(signum)
                except (NotImplementedError, RuntimeError):
                    pass
            for signum, previous_handler in old_handlers.items():
                signal.signal(signum, previous_handler)
            if heartbeat is not None:
                await heartbeat.stop()
        return stop_requested

    print(f"xAgent Feishu channel ready (model={runner.agent.model}).")
    print(f"Connecting to Feishu (app_id={feishu_runtime_config.app_id})...")
    try:
        stop_requested = asyncio.run(_run_daemon())
    except KeyboardInterrupt:
        stop_requested = True
    except RuntimeError as exc:
        print(f"{exc}")
        return 1

    if stop_requested:
        print("Feishu channel stopped.")
    return 0


def _run_weixin_channel(args: argparse.Namespace, config: dict[str, Any]) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        from ...integrations.weixin import WeixinAdapter, WeixinAdapterConfig
    except ImportError as exc:  # pragma: no cover - defensive
        print(f"Failed to import Weixin adapter: {exc}")
        return 1

    weixin_data = weixin_config(config)
    if not weixin_data:
        print("Weixin channel is not configured. Run: xagent channel weixin setup")
        return 1

    try:
        weixin_runtime_config = WeixinAdapterConfig.from_dict(weixin_data)
    except Exception as exc:
        print(f"Invalid Weixin channel config: {exc}")
        return 1

    runner = BaseAgentRunner(config_dir=getattr(args, "config_dir", None))
    adapter = WeixinAdapter(
        agent=runner.agent,
        config=weixin_runtime_config,
        runtime_dir=runner.config_dir,
    )

    async def _run_daemon() -> bool:
        heartbeat = create_runtime_heartbeat(
            runner.agent,
            config.get("runtime") if isinstance(config, dict) else None,
            logger_=logging.getLogger(__name__),
        )
        stop_requested = False
        loop = asyncio.get_running_loop()
        old_handlers: dict[int, object] = {}
        signal_handlers: list[int] = []

        def _request_stop() -> None:
            nonlocal stop_requested
            stop_requested = True
            adapter._stop_event.set()

        def _handle_stop(_signum: int, _frame) -> None:
            loop.call_soon_threadsafe(_request_stop)

        for signum in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
            if signum is None:
                continue
            try:
                loop.add_signal_handler(signum, _request_stop)
                signal_handlers.append(signum)
            except (NotImplementedError, RuntimeError):
                old_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, _handle_stop)

        try:
            if heartbeat is not None:
                await heartbeat.start()
            await adapter.run()
        finally:
            for signum in signal_handlers:
                try:
                    loop.remove_signal_handler(signum)
                except (NotImplementedError, RuntimeError):
                    pass
            for signum, previous_handler in old_handlers.items():
                signal.signal(signum, previous_handler)
            if heartbeat is not None:
                await heartbeat.stop()
        return stop_requested

    print(f"xAgent Weixin channel ready (model={runner.agent.model}).")
    print(f"Connecting to Weixin iLink (account_id={weixin_runtime_config.account_id})...")
    try:
        stop_requested = asyncio.run(_run_daemon())
    except KeyboardInterrupt:
        stop_requested = True
    except RuntimeError as exc:
        print(f"{exc}")
        return 1

    if stop_requested:
        print("Weixin channel stopped.")
    return 0


def _run_channel(channel: str, args: argparse.Namespace, config: dict[str, Any]) -> int:
    if channel == CHANNEL_API:
        return _run_api_channel(args, config)
    if channel == CHANNEL_FEISHU:
        return _run_feishu_channel(args, config)
    if channel == CHANNEL_WEIXIN:
        return _run_weixin_channel(args, config)
    print(f"Unknown channel: {channel}")
    return 1


def handle_run_channel_internal(args: argparse.Namespace) -> int:
    try:
        config = load_runtime_config(args)
    except ChannelSelectionError as exc:
        return _handle_channel_error(exc)
    return _run_channel(args.channel, args, config)


def handle_run(args: argparse.Namespace) -> int:
    try:
        channels, config = _select_channels(args, default="auto")
    except ChannelSelectionError as exc:
        return _handle_channel_error(exc)

    if len(channels) == 1:
        return _run_channel(channels[0], args, config)

    processes: list[subprocess.Popen] = []
    try:
        for channel in channels:
            print(f"Starting {channel} channel in foreground...")
            process = subprocess.Popen(_channel_command(channel, args))
            processes.append(process)
        while processes:
            for process in list(processes):
                return_code = process.poll()
                if return_code is not None:
                    processes.remove(process)
                    if return_code != 0:
                        return return_code
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("Stopping foreground channels...")
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        return 0
    return 0


def _start_background_channels(args: argparse.Namespace, channels: list[str]) -> int:
    ok = True
    config_dir = runtime_dir(args)
    for channel in channels:
        if not _start_background_channel(args, channel=channel, config_dir=config_dir):
            ok = False
    return 0 if ok else 1


def _start_background_channel(args: argparse.Namespace, *, channel: str, config_dir: Path | None = None) -> bool:
    runtime_root = config_dir or runtime_dir(args)
    paths = managed_paths(runtime_root, channel)
    result = start_background(
        _channel_command(channel, args),
        pid_path=paths.pid_path,
        log_path=paths.log_path,
    )
    if result.ok:
        print(f"Started {channel} channel in background (pid={result.pid}).")
        print(f"Logs: {paths.log_path}")
        return True

    print(f"Failed to start {channel} channel: {result.error}")
    if result.recent_output:
        print(result.recent_output)
    return False


def handle_start(args: argparse.Namespace) -> int:
    try:
        channels, _config = _select_channels(args, default="auto")
    except ChannelSelectionError as exc:
        return _handle_channel_error(exc)

    return _start_background_channels(args, channels)


def handle_stop(args: argparse.Namespace) -> int:
    try:
        channels, _config = _select_channels(args, default="auto")
    except ChannelSelectionError as exc:
        return _handle_channel_error(exc)

    ok = True
    config_dir = runtime_dir(args)
    for channel in channels:
        paths = managed_paths(config_dir, channel)
        stopped, message = stop_managed_process(paths.pid_path)
        ok = ok and stopped
        print(f"{channel}: {message}")
    return 0 if ok else 1


def handle_restart(args: argparse.Namespace) -> int:
    try:
        channels, _config = _select_channels(args, default="auto")
    except ChannelSelectionError as exc:
        return _handle_channel_error(exc)

    ok = True
    config_dir = runtime_dir(args)
    restart_values = dict(vars(args))

    for channel in channels:
        paths = managed_paths(config_dir, channel)
        stopped, message = stop_managed_process(paths.pid_path)
        print(f"{channel}: {message}")
        if not stopped:
            ok = False
            continue
        restart_values["channels"] = [channel]
        restart_args = argparse.Namespace(**restart_values)
        if not _start_background_channel(restart_args, channel=channel, config_dir=config_dir):
            ok = False

    return 0 if ok else 1


def handle_status(args: argparse.Namespace) -> int:
    try:
        channels, _config = _select_channels(args, default="auto")
    except ChannelSelectionError as exc:
        return _handle_channel_error(exc)

    config_dir = runtime_dir(args)
    rows: list[dict[str, Any]] = []
    for channel in channels:
        paths = managed_paths(config_dir, channel)
        pid = running_pid(paths.pid_path)
        rows.append({
            "channel": channel,
            "status": "running" if pid is not None else "stopped",
            "pid": pid,
            "pid_path": str(paths.pid_path),
            "log_path": str(paths.log_path),
        })

    if getattr(args, "json_output", False):
        print(json.dumps({"channels": rows}, indent=2, sort_keys=True))
        return 0

    for row in rows:
        pid_text = f" pid={row['pid']}" if row["pid"] is not None else ""
        print(f"{row['channel']}: {row['status']}{pid_text}")
        print(f"  pid: {row['pid_path']}")
        print(f"  log: {row['log_path']}")
    return 0


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
    if getattr(args, "follow", False):
        raw_channels = _channel_arg_values(args)
        explicit_tokens = [
            token.strip().lower()
            for raw_channel in (raw_channels or [])
            for token in str(raw_channel).split(",")
            if token.strip()
        ]
        if len(explicit_tokens) != 1 or explicit_tokens[0] not in {CHANNEL_API, CHANNEL_FEISHU, CHANNEL_WEIXIN}:
            print("--follow requires an explicit single channel")
            return 1

    try:
        channels, _config = _select_channels(args, default="auto")
    except ChannelSelectionError as exc:
        return _handle_channel_error(exc)

    if getattr(args, "follow", False) and len(channels) != 1:
        print("--follow requires exactly one channel")
        return 1

    config_dir = runtime_dir(args)
    for index, channel in enumerate(channels):
        paths = managed_paths(config_dir, channel)
        if len(channels) > 1:
            if index:
                print("")
            print(f"==> {channel}: {paths.log_path} <==")
        output = tail_text(paths.log_path, max_lines=max(1, int(args.lines)))
        if output:
            print(output)
        elif not paths.log_path.exists():
            print(f"No log file: {paths.log_path}")

    if getattr(args, "follow", False):
        _follow_log(managed_paths(config_dir, channels[0]).log_path)
    return 0


def handle_observe(args: argparse.Namespace) -> int:
    metadata = None
    if args.metadata:
        try:
            metadata = json.loads(args.metadata)
        except json.JSONDecodeError as exc:
            print(f"Invalid metadata JSON: {exc}")
            return 1
        if not isinstance(metadata, dict):
            print("--metadata must be a JSON object")
            return 1

    runner = BaseAgentRunner(config_dir=args.config_dir)

    async def _run_observe():
        result = await runner.agent.observe(
            context=args.text,
            source=args.source,
            event_type=args.event_type,
            metadata=metadata,
        )
        if hasattr(result, "model_dump"):
            print(json.dumps(result.model_dump(), indent=2, sort_keys=True))
        else:
            print(result)

    asyncio.run(_run_observe())
    return 0


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
        BaseAgentRunner(config_dir=args.config_dir)
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

        if not getattr(args, "yes", False):
            print("Refusing to clear memory without --yes")
            return 1
        target = scope_root
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        print(f"Cleared memory scope: {getattr(args, 'scope', 'all')}")
        return 0

    print(f"Unknown memory command: {args.memory_command}")
    return 1


def handle_messages(args: argparse.Namespace) -> int:
    runner = BaseAgentRunner(config_dir=args.config_dir)
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
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0

        if args.messages_command == "clear":
            if not getattr(args, "yes", False):
                print("Refusing to clear messages without --yes")
                return 1
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
    if CHANNEL_FEISHU in channels:
        data = feishu_config(config)
        if data.get("app_id") and data.get("app_secret"):
            print("Feishu: configured")
        else:
            print("Feishu: missing app_id/app_secret")
            ok = False
    if CHANNEL_WEIXIN in channels:
        data = weixin_config(config)
        if data.get("account_id"):
            print("Weixin: configured")
        else:
            print("Weixin: missing account_id")
            ok = False
    if args.online:
        print("Online checks are not implemented yet.")
    return 0 if ok else 1


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
    config_dir = Path(BaseAgentConfig.DEFAULT_CONFIG_DIR).expanduser().resolve()
    initialized = _runtime_is_initialized(config_dir)

    print("xAgent")
    print(f"Runtime dir: {config_dir}")
    print("")
    if not initialized:
        print("Quick start:")
        print("  xagent init")
        print("")
        print("After setup:")
        print("  xagent chat")
        print("  xagent web")
        print("  xagent channel")
    else:
        print("Quick start:")
        print("  xagent chat")
        print("  xagent web")
        print("  xagent channel")
    print("")
    print("Use 'xagent --help' to see all commands.")


def _xagent_version_text() -> str:
    try:
        from xagent.__version__ import __version__
    except Exception:  # pragma: no cover - defensive
        return "unknown"
    return __version__


def _launcher_args(**kwargs: Any) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)