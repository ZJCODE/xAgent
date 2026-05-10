import argparse
import asyncio
import logging
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Tuple

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
        print(f"🤖 Agent: {self.agent.name}")
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


def _default_config_yaml(schema: bool = False) -> str:
    config_yaml = """agent:
  name: "starter"
  provider:
    base_url: "https://api.openai.com/v1"
    api_key: "your_api_key_here"
    model: "gpt-5.4-mini"
"""
    if not schema:
        return config_yaml

    return config_yaml + """
  output_schema:
    class_name: "WeatherReport"
    fields:
      location:
        type: "str"
        description: "Location name"
      temperature_celsius:
        type: "int"
        description: "Temperature in degrees Celsius"
      condition:
        type: "str"
        description: "Short weather condition summary"
"""


def _default_identity_markdown() -> str:
    return """# Identity

You are a helpful assistant.
Answer clearly, keep responses practical, and adapt to the user's language.
Be concise by default, and add detail when it improves the answer.
"""


def init_agent_directory(
    config_dir: Optional[str] = None,
    *,
    force: bool = False,
    schema: bool = False,
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

    config_path.write_text(_default_config_yaml(schema=schema), encoding="utf-8")
    identity_path.write_text(_default_identity_markdown(), encoding="utf-8")

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
    result = init_agent_directory(
        args.config_dir,
        force=args.force,
        schema=args.schema,
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
