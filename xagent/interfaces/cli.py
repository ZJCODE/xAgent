import argparse
import asyncio
import getpass
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, Tuple

import yaml

from .base import BaseAgentConfig, BaseAgentRunner
from .channels import (
    CHANNEL_API,
    CHANNEL_FEISHU,
    ChannelSelectionError,
    api_config,
    enabled_channels_from_config,
    feishu_config,
    load_config_file,
    normalize_channel_values,
)
from .processes import managed_paths, running_pid, start_background, stop_managed_process, tail_text


class AgentCLI(BaseAgentRunner):
    """CLI Agent for xAgent."""

    def __init__(
        self,
        config_dir: Optional[str] = None,
        verbose: bool = False,
    ):
        self.verbose = verbose

        if not verbose:
            logging.getLogger().setLevel(logging.CRITICAL)
            logging.getLogger("xagent").setLevel(logging.CRITICAL)
            import warnings

            warnings.filterwarnings("ignore")
        else:
            logging.getLogger().setLevel(logging.INFO)
            logging.getLogger("xagent").setLevel(logging.INFO)

        super().__init__(config_dir=config_dir)

    async def chat_interactive(
        self,
        user_id: Optional[str] = None,
        stream: Optional[bool] = None,
        memory: bool = True,
        private: bool = False,
    ):
        if stream is None:
            stream = not (logging.getLogger().level <= logging.INFO)

        verbose_mode = logging.getLogger().level <= logging.INFO
        user_id = user_id or f"cli_user_{uuid.uuid4().hex[:8]}"

        self._print_banner(
            stream=stream,
            memory=memory,
            private=private,
            verbose_mode=verbose_mode,
        )

        while True:
            try:
                user_input = input("\n👤 You: ").strip()

                if user_input.lower() in ["exit", "quit", "bye"]:
                    print("\n╭───────────────────────────────────────╮")
                    print("│  👋 Thank you for using xAgent CLI!   │")
                    print("│         See you next time! 🚀         │")
                    print("╰───────────────────────────────────────╯")
                    break

                if user_input.lower() == "clear":
                    await self.message_storage.clear_messages()
                    print("🧹 ✨ Global message stream cleared.")
                    continue

                if user_input.lower().startswith("stream "):
                    stream_cmd = user_input.lower().split()
                    if len(stream_cmd) == 2 and stream_cmd[1] in {"on", "off"}:
                        stream = stream_cmd[1] == "on"
                        print(f"{'🌊' if stream else '📄'} ✨ Streaming {'enabled' if stream else 'disabled'}.")
                    else:
                        print("⚠️  Usage: stream on/off")
                    continue

                if user_input.lower().startswith("memory "):
                    memory_cmd = user_input.lower().split()
                    if len(memory_cmd) == 2 and memory_cmd[1] in {"on", "off"}:
                        memory = memory_cmd[1] == "on"
                        print(f"{'🧠' if memory else '🚫'} ✨ Memory {'enabled' if memory else 'disabled'}.")
                    else:
                        print("⚠️  Usage: memory on/off")
                    continue

                if user_input.lower().startswith("private "):
                    private_cmd = user_input.lower().split()
                    if len(private_cmd) == 2 and private_cmd[1] in {"on", "off"}:
                        private = private_cmd[1] == "on"
                        print(f"{'🔒' if private else '🔓'} ✨ Private mode {'enabled' if private else 'disabled'}.")
                    else:
                        print("⚠️  Usage: private on/off")
                    continue

                if user_input.lower() == "help":
                    self._show_help()
                    continue

                if not user_input:
                    print("💭 Please enter a message to chat with the agent.")
                    continue

                response = await self.agent(
                    user_message=user_input,
                    user_id=user_id,
                    stream=stream,
                    enable_memory=memory,
                    private=private,
                )

                if stream and hasattr(response, "__aiter__"):
                    print("🤖 Agent: ", end="", flush=True)
                    async for chunk in response:
                        if chunk:
                            print(chunk, end="", flush=True)
                    print()
                else:
                    print("🤖 Agent: " + str(response))

            except KeyboardInterrupt:
                print("\n\n╭─────────────────────────────────────╮")
                print("│  👋 Session interrupted by user    │")
                print("│      Thank you for using xAgent!   │")
                print("╰─────────────────────────────────────╯")
                break
            except Exception as exc:
                print(f"\n❌ Oops! An error occurred: {exc}")
                if verbose_mode:
                    import traceback

                    traceback.print_exc()

    async def chat_single(
        self,
        message: str,
        user_id: Optional[str] = None,
        stream: bool = False,
        memory: bool = True,
        private: bool = False,
    ):
        user_id = user_id or f"cli_user_{uuid.uuid4().hex[:8]}"
        return await self.agent(
            user_message=message,
            user_id=user_id,
            stream=stream,
            enable_memory=memory,
            private=private,
        )

    def _print_banner(
        self,
        stream: bool,
        memory: bool,
        private: bool,
        verbose_mode: bool,
    ) -> None:
        print("╭" + "─" * 58 + "╮")
        print("│" + " " * 18 + "🤖 Welcome to xAgent CLI!" + " " * 15 + "│")
        print("╰" + "─" * 58 + "╯")

        config_msg = (
            f"📁 Config: {self.config_path}"
            if self.config_path.is_file()
            else f"📁 Config: default values ({self.config_path} not found)"
        )
        print(f"\n{config_msg}")
        print(f"📂 Dir: {self.config_dir}")
        print(f"🧠 Model: {self.agent.model}")

        total_tools = len(self.agent.tools)
        print(f"🛠️  Tools: {total_tools} loaded")

        status_indicators = [
            f"{'🟢' if verbose_mode else '🔇'} Verbose: {'On' if verbose_mode else 'Off'}",
            f"{'🌊' if stream else '📄'} Stream: {'On' if stream else 'Off'}",
            f"{'🧠' if memory else '🚫'} Memory: {'On' if memory else 'Off'}",
            f"{'🔒' if private else '🔓'} Private: {'On' if private else 'Off'}",
        ]
        print(f"⚙️  Status: {' | '.join(status_indicators)}")

        print(f"\n{'─' * 60}")
        print("🚀 Quick Start:")
        print("  • Type your message to chat with the agent")
        print("  • Use 'help' to see all available commands")
        print("  • Use 'exit', 'quit', or 'bye' to end session")
        print("  • Use 'clear' to reset the agent message stream")
        print("  • Use 'stream on/off' to toggle response streaming")
        print("  • Use 'memory on/off' to toggle memory storage")
        print("  • Use 'private on/off' to toggle private mode")
        print("─" * 60)

    def _show_help(self):
        print("\n╭─ 📋 Commands ─────────────────────────────────────────────╮")
        print("│ exit, quit, bye    Exit the chat session                  │")
        print("│ clear              Clear the agent message stream         │")
        print("│ stream on/off      Toggle streaming response mode         │")
        print("│ memory on/off      Toggle memory storage mode             │")
        print("│ private on/off     Toggle private (ephemeral) mode         │")
        print("│ help               Show this help message                 │")
        print("╰───────────────────────────────────────────────────────────╯")

        print("\n╭─ 🔧 Built-in Tools ───────────────────────────────────────╮")
        if self.agent.tools:
            for i, tool_name in enumerate(self.agent.tools.keys(), 1):
                print(f"│ {i:2d}. {tool_name:<50}    │")
        else:
            print("│ No built-in tools available                              │")
        print("╰───────────────────────────────────────────────────────────╯")


@dataclass(frozen=True)
class InitResult:
    """Result for xagent init file generation."""

    config_path: Path
    identity_path: Path
    memory_dir: Path
    messages_dir: Path
    wrote_files: bool
    conflicts: Tuple[Path, ...]


@dataclass(frozen=True)
class InitSelection:
    """Interactive choices used to generate xAgent project files."""

    provider: str
    base_url: str
    api_key: str
    model: str
    identity: str
    search_provider: str = "none"
    search_api_key: str = ""


OPENAI_BASE_URL = "https://api.openai.com/v1"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
CUSTOM_BASE_URL_PLACEHOLDER = "https://api.example.com/v1"
API_KEY_PLACEHOLDER = "your_api_key_here"
BRAVE_SEARCH_API_KEY_PLACEHOLDER = "YOUR_API_KEY"
MODEL_PLACEHOLDER = "your_model_here"

OPENAI_MODELS = (
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "gpt-5.5",
    "Decide later",
)
DEEPSEEK_MODELS = (
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "Decide later",
)
QWEN_MODELS = (
    "qwen3.6-plus",
    "qwen3.6-flash",
    "qwen3.6-max-preview",
    "Decide later",
)
OPENAI_SEARCH_PROVIDERS = (
    "openai",
    "duckduckgo",
    "brave",
    "none",
)
NON_OPENAI_SEARCH_PROVIDERS = (
    "duckduckgo",
    "brave",
    "none",
)


def _default_init_selection() -> InitSelection:
    return InitSelection(
        provider="openai",
        base_url=OPENAI_BASE_URL,
        api_key=API_KEY_PLACEHOLDER,
        model="gpt-5.4-mini",
        identity=_default_identity_markdown(),
        search_provider="openai",
    )


def _weather_output_schema() -> dict:
    return {
        "class_name": "WeatherReport",
        "fields": {
            "location": {
                "type": "str",
                "description": "Location name",
            },
            "temperature_celsius": {
                "type": "int",
                "description": "Temperature in degrees Celsius",
            },
            "condition": {
                "type": "str",
                "description": "Short weather condition summary",
            },
        },
    }


def _config_yaml(selection: InitSelection, schema: bool = False) -> str:
    config = {
        "provider": {
            "name": selection.provider,
            "base_url": selection.base_url,
            "api_key": selection.api_key,
            "model": selection.model,
        },
        "channels": {
            "api": {
                "enabled": True,
                "host": BaseAgentConfig.DEFAULT_HOST,
                "port": BaseAgentConfig.DEFAULT_PORT,
                "web_ui": True,
            }
        },
        "runtime": {
            "default_channel": "api",
        },
    }
    search_config = {"provider": selection.search_provider or "none"}
    if search_config["provider"] == "brave":
        search_config["api_key"] = selection.search_api_key or BRAVE_SEARCH_API_KEY_PLACEHOLDER
    config["search"] = search_config
    if schema:
        config["output_schema"] = _weather_output_schema()
    return yaml.safe_dump(config, sort_keys=False, allow_unicode=False)


def _default_identity_markdown() -> str:
    return """# Identity

You are a helpful assistant.
Answer clearly, keep responses practical, and adapt to the user's language.
Be concise by default, and add detail when it improves the answer.
"""


def _edit_later_identity_markdown() -> str:
    return """# Identity

Describe this agent's role, tone, and behavior here.
"""


def _format_identity_markdown(identity: str) -> str:
    identity = identity.strip()
    if not identity:
        return _edit_later_identity_markdown()
    if identity.startswith("#"):
        return identity + "\n"
    return f"# Identity\n\n{identity}\n"


def _prompt_text(
    prompt: str,
    *,
    default: Optional[str] = None,
    input_func: Callable[[str], str] = input,
) -> str:
    suffix = f" [{default}]" if default else ""
    value = input_func(f"{prompt}{suffix}: ").strip()
    if not value and default is not None:
        return default
    return value


def _prompt_yes_no(
    prompt: str,
    *,
    default: bool = False,
    input_func: Callable[[str], str] = input,
) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        value = input_func(f"{prompt}{suffix}: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Please answer y or n.")


def _select_option(
    title: str,
    options: Sequence[str],
    *,
    default_index: int = 0,
    input_func: Callable[[str], str] = input,
) -> str:
    print(f"\n{title}")
    for index, option in enumerate(options, 1):
        print(f"  {index}. {option}")

    while True:
        raw_choice = input_func("Choose an option number: ").strip()
        if not raw_choice:
            return options[default_index]
        if raw_choice.isdigit():
            choice = int(raw_choice)
            if 1 <= choice <= len(options):
                return options[choice - 1]
        print(f"Please enter a number from 1 to {len(options)}.")


def _select_search_provider(
    provider: str,
    *,
    input_func: Callable[[str], str] = input,
) -> str:
    options = OPENAI_SEARCH_PROVIDERS if provider == "openai" else NON_OPENAI_SEARCH_PROVIDERS
    return _select_option(
        "Search provider",
        options,
        default_index=0,
        input_func=input_func,
    )


def _prompt_multiline_identity(input_func: Callable[[str], str] = input) -> str:
    print("\nEnter agent identity, or leave blank to edit later.")
    print("Type '.' on a new line to save.\n")
    lines = []
    while True:
        line = input_func("> ")
        if line.strip() == ".":
            break
        lines.append(line)
    return _format_identity_markdown("\n".join(lines))


def collect_init_selection(
    *,
    input_func: Callable[[str], str] = input,
    secret_input_func: Callable[[str], str] = getpass.getpass,
) -> InitSelection:
    """Collect init choices from the terminal before writing files."""
    print("\nxAgent init")
    print("Configure the runtime first; files will be written after these choices.")

    provider = _select_option(
        "Provider",
        ("openai", "deepseek", "qwen", "custom"),
        input_func=input_func,
    )

    if provider == "openai":
        selected_model = _select_option(
            "OpenAI model",
            OPENAI_MODELS,
            default_index=1,
            input_func=input_func,
        )
        base_url = OPENAI_BASE_URL
    elif provider == "deepseek":
        selected_model = _select_option(
            "DeepSeek model",
            DEEPSEEK_MODELS,
            default_index=0,
            input_func=input_func,
        )
        base_url = DEEPSEEK_BASE_URL
    elif provider == "qwen":
        selected_model = _select_option(
            "Qwen model",
            QWEN_MODELS,
            default_index=1,
            input_func=input_func,
        )
        base_url = QWEN_BASE_URL
    else:
        selected_model = "Decide later"
        base_url = _prompt_text(
            "Custom provider base URL",
            default=CUSTOM_BASE_URL_PLACEHOLDER,
            input_func=input_func,
        )

    model = MODEL_PLACEHOLDER if selected_model == "Decide later" else selected_model
    api_key = secret_input_func("API key (leave blank to fill in later): ").strip()
    if not api_key:
        api_key = API_KEY_PLACEHOLDER

    search_provider = _select_search_provider(provider, input_func=input_func)
    search_api_key = ""
    if search_provider == "brave":
        search_api_key = secret_input_func("Brave Search API key (leave blank to fill in later): ").strip()
        if not search_api_key:
            search_api_key = BRAVE_SEARCH_API_KEY_PLACEHOLDER

    identity = _prompt_multiline_identity(input_func=input_func)

    return InitSelection(
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        identity=identity,
        search_provider=search_provider,
        search_api_key=search_api_key,
    )


def init_agent_directory(
    config_dir: Optional[str] = None,
    *,
    force: bool = False,
    schema: bool = False,
    selection: Optional[InitSelection] = None,
    clear_runtime_data: bool = False,
) -> InitResult:
    """Create config.yaml, identity.md, and runtime directories."""
    resolved_dir = Path(config_dir or BaseAgentConfig.DEFAULT_CONFIG_DIR).expanduser().resolve()
    resolved_dir.mkdir(parents=True, exist_ok=True)
    config_path = resolved_dir / BaseAgentConfig.CONFIG_FILENAME
    identity_path = resolved_dir / BaseAgentConfig.IDENTITY_FILENAME
    memory_dir = resolved_dir / BaseAgentConfig.MEMORY_DIRNAME
    messages_dir = resolved_dir / BaseAgentConfig.MESSAGE_DIRNAME
    managed_paths = (config_path, identity_path)
    conflicts = tuple(path for path in managed_paths if path.exists())

    if conflicts and not force:
        print("╭─────────────────────────────────────────────────────────╮")
        print("│ xAgent init found existing managed files.              │")
        print("╰─────────────────────────────────────────────────────────╯")
        for path in conflicts:
            print(f"Existing: {path}")
        print("Re-run with --force to overwrite config.yaml and identity.md.")
        return InitResult(
            config_path=config_path,
            identity_path=identity_path,
            memory_dir=memory_dir,
            messages_dir=messages_dir,
            wrote_files=False,
            conflicts=conflicts,
        )

    if clear_runtime_data:
        _clear_runtime_directory(memory_dir)
        _clear_runtime_directory(messages_dir)
    memory_dir.mkdir(parents=True, exist_ok=True)
    messages_dir.mkdir(parents=True, exist_ok=True)

    selection = selection or _default_init_selection()
    config_path.write_text(_config_yaml(selection, schema=schema), encoding="utf-8")
    identity_path.write_text(selection.identity, encoding="utf-8")

    print("╭─────────────────────────────────────────────────────────╮")
    print("│ xAgent project files written successfully.             │")
    print("╰─────────────────────────────────────────────────────────╯")
    print(f"Config: {config_path}")
    print(f"Identity: {identity_path}")
    print(f"Memory: {memory_dir}")
    print(f"Messages: {messages_dir}")
    return InitResult(
        config_path=config_path,
        identity_path=identity_path,
        memory_dir=memory_dir,
        messages_dir=messages_dir,
        wrote_files=True,
        conflicts=(),
    )


def _clear_runtime_directory(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _add_dir_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dir",
        dest="config_dir",
        default=None,
        help="Directory containing config.yaml and identity.md (default: ~/.xagent)",
    )


def _add_channel_argument(
    parser: argparse.ArgumentParser,
    *,
    default_label: str,
) -> None:
    parser.add_argument(
        "--channel",
        dest="channels",
        action="append",
        default=None,
        metavar="CHANNELS",
        help=f"Channel(s) to use: api, feishu, all, or comma-separated values (default: {default_label})",
    )


def _add_api_runtime_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default=None, help="API host override")
    parser.add_argument("--port", type=int, default=None, help="API port override")
    parser.add_argument("--open", action="store_true", dest="open_browser", help="Open the API web UI")
    parser.add_argument(
        "--max-concurrent-chats",
        type=int,
        default=None,
        help="Maximum concurrent chat/observe requests",
    )
    parser.add_argument(
        "--queue-timeout",
        type=float,
        default=None,
        help="Seconds to wait for a chat slot",
    )
    parser.add_argument(
        "--chat-timeout",
        type=float,
        default=None,
        help="Seconds before a chat or observe request times out",
    )


def _hide_subparser_choice(subparsers: argparse._SubParsersAction, name: str) -> None:
    subparsers._choices_actions = [
        action for action in subparsers._choices_actions if action.dest != name
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xagent",
        description="xAgent command line interface",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    init_parser = subparsers.add_parser("init", help="Create config.yaml and identity.md")
    _add_dir_argument(init_parser)
    init_parser.add_argument("--force", action="store_true", help="Overwrite init-managed files")
    init_parser.add_argument("--schema", action="store_true", help="Include a starter output_schema example")
    init_parser.set_defaults(handler=handle_init)

    init_sub = init_parser.add_subparsers(dest="init_target", metavar="<target>")
    init_feishu = init_sub.add_parser("feishu", help="Enable and configure the Feishu channel")
    _add_dir_argument(init_feishu)
    init_feishu.add_argument("--app-id", dest="app_id", default=None, help="Feishu app id (cli_xxx)")
    init_feishu.add_argument("--app-secret", dest="app_secret", default=None, help="Feishu app secret")
    init_feishu.add_argument("--force", action="store_true", help="Overwrite existing channels.feishu config")
    init_feishu.set_defaults(handler=handle_init_feishu)

    run_parser = subparsers.add_parser("run", help="Run one or more channels in the foreground")
    _add_dir_argument(run_parser)
    _add_channel_argument(run_parser, default_label="api")
    _add_api_runtime_arguments(run_parser)
    run_parser.set_defaults(handler=handle_run)

    start_parser = subparsers.add_parser("start", help="Start one or more channels in the background")
    _add_dir_argument(start_parser)
    _add_channel_argument(start_parser, default_label="api")
    _add_api_runtime_arguments(start_parser)
    start_parser.set_defaults(handler=handle_start)

    stop_parser = subparsers.add_parser("stop", help="Stop managed background channels")
    _add_dir_argument(stop_parser)
    _add_channel_argument(stop_parser, default_label="all")
    stop_parser.set_defaults(handler=handle_stop)

    restart_parser = subparsers.add_parser("restart", help="Restart managed background channels")
    _add_dir_argument(restart_parser)
    _add_channel_argument(restart_parser, default_label="all")
    _add_api_runtime_arguments(restart_parser)
    restart_parser.set_defaults(handler=handle_restart)

    status_parser = subparsers.add_parser("status", help="Show managed channel status")
    _add_dir_argument(status_parser)
    _add_channel_argument(status_parser, default_label="all")
    status_parser.add_argument("--json", action="store_true", dest="json_output", help="Print machine-readable JSON")
    status_parser.set_defaults(handler=handle_status)

    logs_parser = subparsers.add_parser("logs", help="Show managed channel logs")
    _add_dir_argument(logs_parser)
    _add_channel_argument(logs_parser, default_label="all")
    logs_parser.add_argument("--lines", type=int, default=80, help="Number of trailing log lines to print")
    logs_parser.add_argument("--follow", "-f", action="store_true", help="Follow log output")
    logs_parser.set_defaults(handler=handle_logs)

    chat_parser = subparsers.add_parser("chat", help="Chat with the configured agent")
    chat_parser.add_argument("message", nargs="?", help="Single message to send; omit for interactive chat")
    _add_dir_argument(chat_parser)
    chat_parser.add_argument("--user-id", dest="user_id", default=None, help="Speaker identifier")
    chat_parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    chat_parser.add_argument(
        "--stream",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable response streaming",
    )
    chat_parser.add_argument(
        "--memory",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable or disable memory tools",
    )
    chat_parser.add_argument("--private", action="store_true", help="Use ephemeral private mode")
    chat_parser.set_defaults(handler=handle_chat)

    observe_parser = subparsers.add_parser("observe", help="Ingest context without generating a reply")
    observe_parser.add_argument("text", help="Observation text to store")
    _add_dir_argument(observe_parser)
    observe_parser.add_argument("--source", default="cli", help="Observation source label")
    observe_parser.add_argument("--event-type", default="observation", help="Observation event type")
    observe_parser.add_argument("--metadata", default=None, help="JSON object with observation metadata")
    observe_parser.set_defaults(handler=handle_observe)

    config_parser = subparsers.add_parser("config", help="Show or validate config.yaml")
    config_sub = config_parser.add_subparsers(dest="config_command", metavar="<subcommand>")
    config_sub.required = True
    for command_name in ("show", "validate", "path"):
        config_cmd = config_sub.add_parser(command_name, help=f"{command_name} config.yaml")
        _add_dir_argument(config_cmd)
        config_cmd.set_defaults(handler=handle_config)

    identity_parser = subparsers.add_parser("identity", help="Show identity.md information")
    identity_sub = identity_parser.add_subparsers(dest="identity_command", metavar="<subcommand>")
    identity_sub.required = True
    for command_name in ("show", "path"):
        identity_cmd = identity_sub.add_parser(command_name, help=f"{command_name} identity.md")
        _add_dir_argument(identity_cmd)
        identity_cmd.set_defaults(handler=handle_identity)

    memory_parser = subparsers.add_parser("memory", help="Inspect or clear long-term memory files")
    memory_sub = memory_parser.add_subparsers(dest="memory_command", metavar="<subcommand>")
    memory_sub.required = True
    for command_name in ("stats", "list", "clear"):
        memory_cmd = memory_sub.add_parser(command_name, help=f"{command_name} memory")
        _add_dir_argument(memory_cmd)
        memory_cmd.add_argument("--scope", default="all", choices=("daily", "weekly", "monthly", "yearly", "all"))
        memory_cmd.add_argument("--yes", action="store_true", help="Confirm destructive operations")
        memory_cmd.set_defaults(handler=handle_memory)
    memory_show = memory_sub.add_parser("show", help="Show a memory markdown file by relative path")
    _add_dir_argument(memory_show)
    memory_show.add_argument("path", help="Relative path inside memory/")
    memory_show.set_defaults(handler=handle_memory)
    memory_search = memory_sub.add_parser("search", help="Search memory markdown files")
    _add_dir_argument(memory_search)
    memory_search.add_argument("query", help="Search query")
    memory_search.add_argument("--scope", default="all", choices=("daily", "weekly", "monthly", "yearly", "all"))
    memory_search.set_defaults(handler=handle_memory)

    messages_parser = subparsers.add_parser("messages", help="Inspect or clear the message stream")
    messages_sub = messages_parser.add_subparsers(dest="messages_command", metavar="<subcommand>")
    messages_sub.required = True
    messages_stats = messages_sub.add_parser("stats", help="Show message stream statistics")
    _add_dir_argument(messages_stats)
    messages_stats.set_defaults(handler=handle_messages)
    messages_list = messages_sub.add_parser("list", help="List recent messages")
    _add_dir_argument(messages_list)
    messages_list.add_argument("--count", type=int, default=20, help="Number of recent messages")
    messages_list.add_argument("--offset", type=int, default=0, help="Number of recent messages to skip")
    messages_list.set_defaults(handler=handle_messages)
    messages_clear = messages_sub.add_parser("clear", help="Clear all stored messages")
    _add_dir_argument(messages_clear)
    messages_clear.add_argument("--yes", action="store_true", help="Confirm clearing the message stream")
    messages_clear.set_defaults(handler=handle_messages)

    doctor_parser = subparsers.add_parser("doctor", help="Check local xAgent readiness")
    _add_dir_argument(doctor_parser)
    _add_channel_argument(doctor_parser, default_label="all")
    doctor_parser.add_argument("--online", action="store_true", help="Include network/model checks")
    doctor_parser.set_defaults(handler=handle_doctor)

    version_parser = subparsers.add_parser("version", help="Show xAgent version")
    version_parser.set_defaults(handler=handle_version)

    internal_run = subparsers.add_parser("_run-channel", help=argparse.SUPPRESS)
    internal_run.add_argument("channel", choices=(CHANNEL_API, CHANNEL_FEISHU))
    _add_dir_argument(internal_run)
    _add_api_runtime_arguments(internal_run)
    internal_run.set_defaults(handler=handle_run_channel_internal)
    _hide_subparser_choice(subparsers, "_run-channel")

    server_parser = subparsers.add_parser("server", help=argparse.SUPPRESS, add_help=False)
    server_parser.add_argument("-h", "--help", action="store_true", dest="legacy_help")
    server_parser.add_argument("legacy_args", nargs=argparse.REMAINDER)
    server_parser.set_defaults(handler=handle_legacy_server)
    _hide_subparser_choice(subparsers, "server")

    feishu_parser = subparsers.add_parser("feishu", help=argparse.SUPPRESS, add_help=False)
    feishu_parser.add_argument("-h", "--help", action="store_true", dest="legacy_help")
    feishu_parser.add_argument("legacy_args", nargs=argparse.REMAINDER)
    feishu_parser.set_defaults(handler=handle_legacy_feishu)
    _hide_subparser_choice(subparsers, "feishu")

    return parser


def handle_init(args: argparse.Namespace) -> int:
    resolved_dir = Path(args.config_dir or BaseAgentConfig.DEFAULT_CONFIG_DIR).expanduser().resolve()
    conflicts = tuple(
        path for path in (
            resolved_dir / BaseAgentConfig.CONFIG_FILENAME,
            resolved_dir / BaseAgentConfig.IDENTITY_FILENAME,
        )
        if path.exists()
    )
    if conflicts and not args.force:
        result = init_agent_directory(
            args.config_dir,
            force=args.force,
            schema=args.schema,
        )
        return 0 if result.wrote_files else 1

    clear_runtime_data = False
    if args.force:
        clear_runtime_data = _prompt_yes_no(
            "Clear existing memory/ and messages/ data as part of init --force?",
            default=False,
        )

    selection = collect_init_selection()
    result = init_agent_directory(
        args.config_dir,
        force=args.force,
        schema=args.schema,
        selection=selection,
        clear_runtime_data=clear_runtime_data,
    )
    return 0 if result.wrote_files else 1


def handle_chat(args: argparse.Namespace) -> int:
    agent_cli = AgentCLI(config_dir=args.config_dir, verbose=args.verbose)

    if args.message is None:
        asyncio.run(
            agent_cli.chat_interactive(
                user_id=args.user_id,
                stream=args.stream,
                memory=args.memory,
                private=args.private,
            )
        )
        return 0

    stream = bool(args.stream) if args.stream is not None else False

    async def run_single_message():
        response = await agent_cli.chat_single(
            message=args.message,
            user_id=args.user_id,
            stream=stream,
            memory=args.memory,
            private=args.private,
        )
        if stream and hasattr(response, "__aiter__"):
            async for chunk in response:
                if chunk:
                    print(chunk, end="", flush=True)
            print()
        else:
            print(response)

    asyncio.run(run_single_message())
    return 0


def handle_server(args: argparse.Namespace) -> int:
    from .server import AgentHTTPServer

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


def _runtime_dir(args: argparse.Namespace) -> Path:
    raw_dir = getattr(args, "config_dir", None) or BaseAgentConfig.DEFAULT_CONFIG_DIR
    return Path(raw_dir).expanduser().resolve()


def _config_path(args: argparse.Namespace) -> Path:
    return _runtime_dir(args) / BaseAgentConfig.CONFIG_FILENAME


def _identity_path(args: argparse.Namespace) -> Path:
    return _runtime_dir(args) / BaseAgentConfig.IDENTITY_FILENAME


def _load_runtime_config(args: argparse.Namespace) -> dict[str, Any]:
    return load_config_file(_runtime_dir(args))


def _select_channels(args: argparse.Namespace, *, default: str) -> tuple[list[str], dict[str, Any]]:
    config = _load_runtime_config(args)
    channels = normalize_channel_values(getattr(args, "channels", None), default=default, config=config)
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
    enable_web = bool(api_cfg.get("web_ui", True))
    server_kwargs: dict[str, Any] = {
        "config_dir": getattr(args, "config_dir", None),
        "enable_web": enable_web,
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
    open_browser = bool(getattr(args, "open_browser", False) and enable_web)
    return server_kwargs, host, port, open_browser


def _run_api_channel(args: argparse.Namespace, config: dict[str, Any]) -> int:
    from .server import AgentHTTPServer

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
        from ..integrations.feishu import FeishuAdapter, FeishuAdapterConfig
    except ImportError as exc:  # pragma: no cover - defensive
        print(f"Failed to import Feishu adapter: {exc}")
        return 1

    feishu_data = feishu_config(config)
    if not feishu_data:
        print("Feishu channel is not configured. Run: xagent init feishu")
        return 1

    try:
        feishu_runtime_config = FeishuAdapterConfig.from_dict(feishu_data)
    except Exception as exc:
        print(f"Invalid Feishu channel config: {exc}")
        return 1

    runner = BaseAgentRunner(config_dir=getattr(args, "config_dir", None))
    adapter = FeishuAdapter(agent=runner.agent, config=feishu_runtime_config)

    stop_requested = False
    old_handlers: dict[int, object] = {}

    def _handle_stop(signum: int, _frame) -> None:
        nonlocal stop_requested
        stop_requested = True
        adapter._safe_stop()

    for signum in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if signum is None:
            continue
        old_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, _handle_stop)

    print(f"xAgent Feishu channel ready (model={runner.agent.model}).")
    print(f"Connecting to Feishu (app_id={feishu_runtime_config.app_id})...")
    try:
        adapter.run_blocking()
    except KeyboardInterrupt:
        stop_requested = True
    except RuntimeError as exc:
        print(f"{exc}")
        return 1
    finally:
        for signum, previous_handler in old_handlers.items():
            signal.signal(signum, previous_handler)

    if stop_requested:
        print("Feishu channel stopped.")
    return 0


def _run_channel(channel: str, args: argparse.Namespace, config: dict[str, Any]) -> int:
    if channel == CHANNEL_API:
        return _run_api_channel(args, config)
    if channel == CHANNEL_FEISHU:
        return _run_feishu_channel(args, config)
    print(f"Unknown channel: {channel}")
    return 1


def handle_run_channel_internal(args: argparse.Namespace) -> int:
    try:
        config = _load_runtime_config(args)
    except ChannelSelectionError as exc:
        return _handle_channel_error(exc)
    return _run_channel(args.channel, args, config)


def handle_run(args: argparse.Namespace) -> int:
    try:
        channels, config = _select_channels(args, default=CHANNEL_API)
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


def handle_start(args: argparse.Namespace) -> int:
    try:
        channels, _config = _select_channels(args, default=CHANNEL_API)
    except ChannelSelectionError as exc:
        return _handle_channel_error(exc)

    ok = True
    config_dir = _runtime_dir(args)
    for channel in channels:
        paths = managed_paths(config_dir, channel)
        result = start_background(
            _channel_command(channel, args),
            pid_path=paths.pid_path,
            log_path=paths.log_path,
        )
        if result.ok:
            print(f"Started {channel} channel in background (pid={result.pid}).")
            print(f"Logs: {paths.log_path}")
            continue

        ok = False
        print(f"Failed to start {channel} channel: {result.error}")
        if result.recent_output:
            print(result.recent_output)
    return 0 if ok else 1


def handle_stop(args: argparse.Namespace) -> int:
    try:
        channels, _config = _select_channels(args, default="all")
    except ChannelSelectionError as exc:
        return _handle_channel_error(exc)

    ok = True
    config_dir = _runtime_dir(args)
    for channel in channels:
        paths = managed_paths(config_dir, channel)
        stopped, message = stop_managed_process(paths.pid_path)
        ok = ok and stopped
        print(f"{channel}: {message}")
    return 0 if ok else 1


def handle_restart(args: argparse.Namespace) -> int:
    stop_code = handle_stop(args)
    start_code = handle_start(args)
    return 0 if stop_code == 0 and start_code == 0 else 1


def handle_status(args: argparse.Namespace) -> int:
    try:
        channels, _config = _select_channels(args, default="all")
    except ChannelSelectionError as exc:
        return _handle_channel_error(exc)

    config_dir = _runtime_dir(args)
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
    try:
        channels, _config = _select_channels(args, default="all")
    except ChannelSelectionError as exc:
        return _handle_channel_error(exc)

    if getattr(args, "follow", False) and len(channels) != 1:
        print("--follow requires exactly one channel")
        return 1

    config_dir = _runtime_dir(args)
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


def handle_init_feishu(args: argparse.Namespace) -> int:
    config_path = _config_path(args)
    if not config_path.is_file():
        print(f"Config not found: {config_path}")
        print("Run: xagent init")
        return 1

    try:
        with config_path.open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        print(f"Invalid YAML in {config_path}: {exc}")
        return 1
    if not isinstance(config, dict):
        print(f"Configuration must be a mapping: {config_path}")
        return 1

    channels_cfg = config.setdefault("channels", {})
    if not isinstance(channels_cfg, dict):
        print("channels must be a dictionary")
        return 1
    if "feishu" in channels_cfg and not args.force:
        print("channels.feishu already exists. Use --force to overwrite.")
        return 1

    print("")
    print("Feishu setup guide:\n")
    print("1. Create an agent: https://open.feishu.cn/page/launcher")
    print("2. Copy your App ID and App Secret.")
    print("")

    app_id = args.app_id or input("Feishu App ID: ").strip()
    if not app_id:
        print("App ID is required.")
        return 1
    app_secret = args.app_secret or getpass.getpass("Feishu App Secret: ").strip()
    if not app_secret:
        print("App Secret is required.")
        return 1

    api_cfg = channels_cfg.setdefault("api", {})
    if isinstance(api_cfg, dict):
        api_cfg.setdefault("enabled", True)
        api_cfg.setdefault("host", BaseAgentConfig.DEFAULT_HOST)
        api_cfg.setdefault("port", BaseAgentConfig.DEFAULT_PORT)
        api_cfg.setdefault("web_ui", True)

    channels_cfg["feishu"] = {
        "enabled": True,
        "app_id": app_id,
        "app_secret": app_secret,
        "log_level": "info",
        "stream": False,
        "enable_memory": True,
        "group_history_count": 10,
        "show_sender_ids": True,
    }
    config.setdefault("runtime", {}).setdefault("default_channel", "api")

    config_path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=False), encoding="utf-8")
    print(f"\nUpdated {config_path} with channels.feishu\n")
    print("===== Finish setup in the Feishu Developer Console =====\n")
    print("1. Open your agent: https://open.feishu.cn/app")
    print("2. Add extra permissions:")
    print("  - im:message.group_msg (for group chats)")
    print("  - im:message.group_at_msg.include_bot:readonly (for group @mentions from users and bots)")
    print("  - contact:user.base:readonly (for user display names)")
    print("  - admin:app.info:readonly (for other bot or agent display names)")
    print("\nRun: `xagent start --channel feishu` or `xagent start --channel all` to start your bot.\n")
    print("======================================================\n")
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
    path = _config_path(args)
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
    path = _identity_path(args)
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
    return _runtime_dir(args) / BaseAgentConfig.MEMORY_DIRNAME


def _memory_scope_root(args: argparse.Namespace) -> Path:
    scope = getattr(args, "scope", "all")
    root = _memory_root(args)
    return root if scope == "all" else root / scope


def _safe_memory_path(args: argparse.Namespace, relative_path: str) -> Optional[Path]:
    root = _memory_root(args).resolve()
    requested = (root / relative_path).resolve()
    if not requested.is_relative_to(root):
        return None
    return requested


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
        files = sorted(path for path in scope_root.rglob("*.md") if path.is_file()) if scope_root.exists() else []
        for path in files:
            print(path.relative_to(root))
        return 0

    if args.memory_command == "show":
        path = _safe_memory_path(args, args.path)
        if path is None:
            print("Access denied: memory path escapes memory root")
            return 1
        if not path.is_file() or path.suffix != ".md":
            print(f"Memory file not found: {path}")
            return 1
        print(path.read_text(encoding="utf-8", errors="replace"), end="")
        return 0

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
    config_dir = _runtime_dir(args)
    config_path = config_dir / BaseAgentConfig.CONFIG_FILENAME
    identity_path = config_dir / BaseAgentConfig.IDENTITY_FILENAME
    ok = True

    print(f"Runtime dir: {config_dir}")
    if config_path.is_file():
        print(f"Config: ok ({config_path})")
    else:
        print(f"Config: missing ({config_path})")
        ok = False

    if identity_path.is_file() and identity_path.read_text(encoding="utf-8").strip():
        print(f"Identity: ok ({identity_path})")
    else:
        print(f"Identity: missing or empty ({identity_path})")
        ok = False

    try:
        config = load_config_file(config_dir)
        channels = normalize_channel_values(getattr(args, "channels", None), default="all", config=config)
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


def handle_legacy_server(_args: argparse.Namespace) -> int:
    print("`xagent server` has been replaced by `xagent run --channel api` for foreground use")
    print("or `xagent start --channel api` for managed background use.")
    return 1


def handle_legacy_feishu(_args: argparse.Namespace) -> int:
    print("`xagent feishu ...` has been replaced by channel lifecycle commands:")
    print("  xagent init feishu")
    print("  xagent run --channel feishu")
    print("  xagent start --channel feishu")
    print("  xagent stop --channel feishu")
    print("  xagent status --channel feishu")
    return 1


_FEISHU_CONFIG_FILENAME = "feishu.yaml"
_FEISHU_DIRNAME = "feishu"
_FEISHU_PID_FILENAME = "feishu.pid"
_FEISHU_LOG_FILENAME = "feishu.log"
_FEISHU_STARTUP_TIMEOUT = 2.0
_FEISHU_STOP_TIMEOUT = 5.0
_FEISHU_STOP_POLL_INTERVAL = 0.1

_FEISHU_CONFIG_TEMPLATE = """\
# Feishu (Lark) bot adapter configuration.
# Docs: xagent/integrations/feishu/README.md
#
# Routing is hardcoded and behaves like a real human teammate:
#   - Direct chat (p2p)        -> reply
#   - Group, bot @mentioned    -> pull recent history, then reply
#   - Group, not @mentioned    -> ignore
#
# Use ${{ENV_VAR}} to interpolate from environment variables.
app_id: {app_id}
app_secret: {app_secret}

# Optional knobs (safe to delete):
# log_level: info        # debug | info | warn | error
# stream: false          # stream tokens to a Feishu card
# enable_memory: true    # forward to agent long-term memory
# group_history_count: 10 # recent group/topic messages to read on @mention
"""


def _feishu_config_path(args: argparse.Namespace) -> Path:
    override = getattr(args, "feishu_config", None)
    if override:
        return Path(override).expanduser().resolve()
    return _feishu_dir(args) / _FEISHU_CONFIG_FILENAME


def _feishu_runtime_dir(args: argparse.Namespace) -> Path:
    raw_dir = getattr(args, "config_dir", None) or BaseAgentConfig.DEFAULT_CONFIG_DIR
    return Path(raw_dir).expanduser().resolve()


def _feishu_dir(args: argparse.Namespace) -> Path:
    return _feishu_runtime_dir(args) / _FEISHU_DIRNAME


def _feishu_pid_path(args: argparse.Namespace) -> Path:
    return _feishu_dir(args) / _FEISHU_PID_FILENAME


def _feishu_log_path(args: argparse.Namespace) -> Path:
    return _feishu_dir(args) / _FEISHU_LOG_FILENAME


def _read_feishu_pid(pid_path: Path) -> Optional[int]:
    if not pid_path.is_file():
        return None
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def _write_feishu_pid(pid_path: Path, pid: int) -> None:
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(f"{pid}\n", encoding="utf-8")


def _remove_feishu_pid(pid_path: Path, expected_pid: Optional[int] = None) -> None:
    if not pid_path.exists():
        return
    if expected_pid is not None:
        current_pid = _read_feishu_pid(pid_path)
        if current_pid is not None and current_pid != expected_pid:
            return
    try:
        pid_path.unlink()
    except OSError:
        pass


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _running_feishu_pid(args: argparse.Namespace, *, current_pid: Optional[int] = None) -> Optional[int]:
    pid_path = _feishu_pid_path(args)
    pid = _read_feishu_pid(pid_path)
    if pid is None:
        _remove_feishu_pid(pid_path)
        return None
    if current_pid is not None and pid == current_pid:
        return pid
    if _pid_is_running(pid):
        return pid
    _remove_feishu_pid(pid_path)
    return None


def _build_feishu_background_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "xagent.interfaces.cli",
        "feishu",
        "start",
        "--foreground-internal",
    ]
    config_dir = getattr(args, "config_dir", None)
    if config_dir:
        command.extend(["--dir", config_dir])
    feishu_config = getattr(args, "feishu_config", None)
    if feishu_config:
        command.extend(["--config", feishu_config])
    return command


def _tail_text(path: Path, max_lines: int = 20) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:]).strip()


def _wait_for_process_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_is_running(pid):
            return True
        time.sleep(_FEISHU_STOP_POLL_INTERVAL)
    return not _pid_is_running(pid)


def _run_feishu_foreground(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        from ..integrations.feishu import FeishuAdapter, FeishuAdapterConfig
    except ImportError as exc:  # pragma: no cover - defensive
        print(f"❌ Failed to import Feishu adapter: {exc}")
        return 1

    config_path = _feishu_config_path(args)
    if not config_path.is_file():
        print(f"❌ Feishu config not found: {config_path}")
        print("   Run: xagent feishu init")
        return 1

    current_pid = os.getpid()
    existing_pid = _running_feishu_pid(args, current_pid=current_pid)
    if existing_pid is not None and existing_pid != current_pid:
        print(f"❌ Feishu is already running (pid={existing_pid}).")
        print(f"   Stop it with: xagent feishu stop")
        return 1

    pid_path = _feishu_pid_path(args)
    _write_feishu_pid(pid_path, current_pid)

    runner = BaseAgentRunner(config_dir=getattr(args, "config_dir", None))
    feishu_config = FeishuAdapterConfig.from_file(config_path)
    adapter = FeishuAdapter(agent=runner.agent, config=feishu_config)

    stop_requested = False
    old_handlers: dict[int, object] = {}

    def _handle_stop(signum: int, _frame) -> None:
        nonlocal stop_requested
        stop_requested = True
        adapter._safe_stop()

    for signum in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if signum is None:
            continue
        old_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, _handle_stop)

    print(f"xAgent Feishu ready (model={runner.agent.model})")
    print(f"Connecting to Feishu (app_id={feishu_config.app_id})...")
    if getattr(args, "foreground", False):
        print("Press Ctrl+C to stop.")

    try:
        adapter.run_blocking()
    except KeyboardInterrupt:
        stop_requested = True
    except RuntimeError as exc:
        print(f"❌ {exc}")
        return 1
    finally:
        for signum, previous_handler in old_handlers.items():
            signal.signal(signum, previous_handler)
        _remove_feishu_pid(pid_path, current_pid)

    if stop_requested and getattr(args, "foreground", False):
        print("\nFeishu adapter stopped.")
    return 0


def _start_feishu_background(args: argparse.Namespace) -> int:
    runtime_dir = _feishu_runtime_dir(args)
    feishu_dir = _feishu_dir(args)
    feishu_dir.mkdir(parents=True, exist_ok=True)

    existing_pid = _running_feishu_pid(args)
    if existing_pid is not None:
        print(f"❌ Feishu is already running in the background (pid={existing_pid}).")
        print(f"   Stop it with: xagent feishu stop")
        return 1

    log_path = _feishu_log_path(args)
    command = _build_feishu_background_command(args)

    with log_path.open("ab") as log_handle:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            print(f"❌ Failed to start Feishu in the background: {exc}")
            return 1

    pid_path = _feishu_pid_path(args)
    _write_feishu_pid(pid_path, process.pid)

    try:
        process.wait(timeout=_FEISHU_STARTUP_TIMEOUT)
    except subprocess.TimeoutExpired:
        print(f"Started Feishu in background (pid={process.pid}).")
        print(f"Logs: {log_path}")
        print(f"Stop: xagent feishu stop")
        return 0

    _remove_feishu_pid(pid_path, process.pid)
    recent_output = _tail_text(log_path)
    print("❌ Feishu exited during startup.")
    if recent_output:
        print(recent_output)
    else:
        print(f"Check logs: {log_path}")
    return process.returncode or 1


def handle_feishu_status(args: argparse.Namespace) -> int:
    runtime_dir = _feishu_runtime_dir(args)
    feishu_dir = _feishu_dir(args)
    config_path = _feishu_config_path(args)
    pid_path = _feishu_pid_path(args)
    log_path = _feishu_log_path(args)
    pid = _running_feishu_pid(args)

    print(f"Feishu dir: {feishu_dir}")
    print(f"Config: {config_path} ({'exists' if config_path.is_file() else 'missing'})")
    print(f"PID file: {pid_path}")
    print(f"Logs: {log_path}")
    if pid is None:
        print("Status: stopped")
        print(f"Start: xagent feishu start --dir {runtime_dir}")
        return 0

    print(f"Status: running (pid={pid})")
    print(f"Stop: xagent feishu stop")
    return 0


def handle_feishu_stop(args: argparse.Namespace) -> int:
    pid_path = _feishu_pid_path(args)
    pid = _running_feishu_pid(args)
    if pid is None:
        print(f"No Feishu process is running for {_feishu_runtime_dir(args)}.")
        return 0

    stop_signal = getattr(signal, "SIGTERM", signal.SIGINT)
    try:
        os.kill(pid, stop_signal)
    except ProcessLookupError:
        _remove_feishu_pid(pid_path, pid)
        print(f"Feishu process {pid} is already stopped.")
        return 0
    except PermissionError as exc:
        print(f"❌ Failed to stop Feishu process {pid}: {exc}")
        return 1

    if _wait_for_process_exit(pid, _FEISHU_STOP_TIMEOUT):
        _remove_feishu_pid(pid_path, pid)
        print(f"Stopped Feishu process {pid}.")
        return 0

    kill_signal = getattr(signal, "SIGKILL", None)
    if kill_signal is not None:
        try:
            os.kill(pid, kill_signal)
        except ProcessLookupError:
            _remove_feishu_pid(pid_path, pid)
            print(f"Stopped Feishu process {pid}.")
            return 0
        except PermissionError as exc:
            print(f"❌ Failed to force-stop Feishu process {pid}: {exc}")
            return 1

        if _wait_for_process_exit(pid, _FEISHU_STOP_TIMEOUT):
            _remove_feishu_pid(pid_path, pid)
            print(f"Force-stopped Feishu process {pid}.")
            return 0

    print(f"❌ Timed out while stopping Feishu process {pid}.")
    return 1


def handle_feishu_init(args: argparse.Namespace) -> int:
    config_path = _feishu_config_path(args)
    config_path.parent.mkdir(parents=True, exist_ok=True)

    if config_path.exists() and not args.force:
        print(f"⚠️  {config_path} already exists. Use --force to overwrite.")
        return 1

    print("")
    print("Feishu setup guide:\n")
    print("1. Create an agent: https://open.feishu.cn/page/launcher\n")
    print("2. Copy your App ID and App Secret.")
    print("")

    app_id = args.app_id or input("Feishu App ID: ").strip()
    if not app_id:
        print("❌ App ID is required.")
        return 1
    app_secret = args.app_secret or getpass.getpass("Feishu App Secret: ").strip()
    if not app_secret:
        print("❌ App Secret is required.")
        return 1

    config_path.write_text(
        _FEISHU_CONFIG_TEMPLATE.format(app_id=app_id, app_secret=app_secret),
        encoding="utf-8",
    )
    print(f"\nWrote {config_path}\n")
    
    print("===== Finish setup in the Feishu Developer Console =====\n")
    print("1. Open your agent: https://open.feishu.cn/app\n")
    print("2. Add extra permissions:")
    print("  - im:message.group_msg (for group chats)")
    print("  - im:message.group_at_msg.include_bot:readonly (for group @mentions from users and bots)")
    print("  - contact:user.base:readonly (for user display names)")
    print("  - admin:app.info:readonly (for other bot or agent display names)")
    print(f"\nRun: `xagent feishu start` to start your bot!\n")
    print("======================================================\n")
    return 0


def handle_feishu_start(args: argparse.Namespace) -> int:
    if getattr(args, "foreground", False) or getattr(args, "feishu_foreground_internal", False):
        return _run_feishu_foreground(args)
    return _start_feishu_background(args)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not hasattr(args, "handler"):
        parser.print_help()
        return 0

    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
