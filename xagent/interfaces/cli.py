import argparse
import asyncio
import getpass
import logging
import sys
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


OPENAI_BASE_URL = "https://api.openai.com/v1"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
CUSTOM_BASE_URL_PLACEHOLDER = "https://api.example.com/v1"
API_KEY_PLACEHOLDER = "your_api_key_here"
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


def _default_init_selection() -> InitSelection:
    return InitSelection(
        provider="openai",
        base_url=OPENAI_BASE_URL,
        api_key=API_KEY_PLACEHOLDER,
        model="gpt-5.4-mini",
        identity=_default_identity_markdown(),
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
            "base_url": selection.base_url,
            "api_key": selection.api_key,
            "model": selection.model,
        }
    }
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


def _prompt_multiline_identity(input_func: Callable[[str], str] = input) -> str:
    print("\nEnter the agent identity, or submit an empty value and finish with '.' to edit later.")
    print("Finish with a single '.' on its own line.")
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

    identity = _prompt_multiline_identity(input_func=input_func)

    return InitSelection(
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        identity=identity,
    )


def init_agent_directory(
    config_dir: Optional[str] = None,
    *,
    force: bool = False,
    schema: bool = False,
    selection: Optional[InitSelection] = None,
) -> InitResult:
    """Create config.yaml and identity.md in the selected xAgent directory."""
    resolved_dir = Path(config_dir or BaseAgentConfig.DEFAULT_CONFIG_DIR).expanduser().resolve()
    resolved_dir.mkdir(parents=True, exist_ok=True)
    config_path = resolved_dir / BaseAgentConfig.CONFIG_FILENAME
    identity_path = resolved_dir / BaseAgentConfig.IDENTITY_FILENAME
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
            wrote_files=False,
            conflicts=conflicts,
        )

    selection = selection or _default_init_selection()
    config_path.write_text(_config_yaml(selection, schema=schema), encoding="utf-8")
    identity_path.write_text(selection.identity, encoding="utf-8")

    print("╭─────────────────────────────────────────────────────────╮")
    print("│ xAgent project files written successfully.             │")
    print("╰─────────────────────────────────────────────────────────╯")
    print(f"Config: {config_path}")
    print(f"Identity: {identity_path}")
    return InitResult(
        config_path=config_path,
        identity_path=identity_path,
        wrote_files=True,
        conflicts=(),
    )


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

    selection = collect_init_selection()
    result = init_agent_directory(
        args.config_dir,
        force=args.force,
        schema=args.schema,
        selection=selection,
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not hasattr(args, "handler"):
        parser.print_help()
        return 0

    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
