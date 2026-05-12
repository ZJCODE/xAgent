import argparse
import asyncio
import getpass
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
from typing import Callable, Optional, Sequence, Tuple

import yaml

from .base import BaseAgentConfig, BaseAgentRunner


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
        }
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

    server_parser = subparsers.add_parser("server", help="Run the HTTP server")
    _add_dir_argument(server_parser)
    server_parser.add_argument("--host", default=None, help="Host to bind to")
    server_parser.add_argument("--port", type=int, default=None, help="Port to bind to")
    server_parser.add_argument("--open", action="store_true", dest="open_browser", help="Open the web UI")
    server_parser.add_argument("--no-web", action="store_true", dest="no_web", help="Disable the built-in web UI")
    server_parser.add_argument(
        "--max-concurrent-chats",
        type=int,
        default=None,
        help="Maximum concurrent chat requests",
    )
    server_parser.add_argument(
        "--queue-timeout",
        type=float,
        default=None,
        help="Seconds to wait for a chat slot",
    )
    server_parser.add_argument(
        "--chat-timeout",
        type=float,
        default=None,
        help="Seconds before a chat request times out",
    )
    server_parser.set_defaults(handler=handle_server)

    feishu_parser = subparsers.add_parser(
        "feishu",
        help="Run the Feishu (Lark) bot adapter using WebSocket long connection",
    )
    feishu_sub = feishu_parser.add_subparsers(dest="feishu_command", metavar="<subcommand>")
    feishu_sub.required = True

    feishu_init = feishu_sub.add_parser("init", help="Create feishu/feishu.yaml in the runtime directory")
    _add_dir_argument(feishu_init)
    feishu_init.add_argument("--app-id", dest="app_id", default=None, help="Feishu app id (cli_xxx)")
    feishu_init.add_argument("--app-secret", dest="app_secret", default=None, help="Feishu app secret")
    feishu_init.add_argument("--force", action="store_true", help="Overwrite existing feishu/feishu.yaml")
    feishu_init.set_defaults(handler=handle_feishu_init)

    feishu_start = feishu_sub.add_parser("start", help="Start the Feishu adapter")
    _add_dir_argument(feishu_start)
    feishu_start.add_argument(
        "--config",
        dest="feishu_config",
        default=None,
        help="Path to feishu.yaml (default: <dir>/feishu/feishu.yaml)",
    )
    feishu_start.add_argument(
        "--foreground",
        "-f",
        action="store_true",
        help="Run in foreground and print logs",
    )
    feishu_start.add_argument(
        "--foreground-internal",
        dest="feishu_foreground_internal",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    feishu_start.set_defaults(handler=handle_feishu_start)

    feishu_stop = feishu_sub.add_parser("stop", help="Stop the Feishu adapter for this runtime directory")
    _add_dir_argument(feishu_stop)
    feishu_stop.set_defaults(handler=handle_feishu_stop)

    feishu_status = feishu_sub.add_parser("status", help="Show Feishu adapter status")
    _add_dir_argument(feishu_status)
    feishu_status.set_defaults(handler=handle_feishu_status)

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
    print("  - contact:user.base:readonly (for user display names)")
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
