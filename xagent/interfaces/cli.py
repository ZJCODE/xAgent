import argparse
import asyncio
import logging
import os
import uuid
from typing import Optional

from dotenv import load_dotenv

from .base import BaseAgentRunner


class AgentCLI(BaseAgentRunner):
    """CLI Agent for xAgent."""

    def __init__(
        self,
        config_path: Optional[str] = None,
        toolkit_path: Optional[str] = None,
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

        super().__init__(config_path, toolkit_path)
        self.config_path = config_path if config_path and os.path.isfile(config_path) else None

    async def chat_interactive(
        self,
        user_id: Optional[str] = None,
        stream: Optional[bool] = None,
        memory: bool = True,
    ):
        if stream is None:
            stream = not (logging.getLogger().level <= logging.INFO)

        verbose_mode = logging.getLogger().level <= logging.INFO
        user_id = user_id or f"cli_user_{uuid.uuid4().hex[:8]}"

        self._print_banner(
            stream=stream,
            memory=memory,
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
        memory: bool = True,
    ):
        user_id = user_id or f"cli_user_{uuid.uuid4().hex[:8]}"
        return await self.agent(
            user_message=message,
            user_id=user_id,
            stream=False,
            enable_memory=memory,
        )

    def _print_banner(
        self,
        stream: bool,
        memory: bool,
        verbose_mode: bool,
    ) -> None:
        print("╭" + "─" * 58 + "╮")
        print("│" + " " * 18 + "🤖 Welcome to xAgent CLI!" + " " * 15 + "│")
        print("╰" + "─" * 58 + "╯")

        config_msg = f"📁 Config: {self.config_path}" if self.config_path else "📁 Config: Default configuration"
        print(f"\n{config_msg}")
        print(f"🤖 Agent: {self.agent.name}")
        print(f"🧠 Model: {self.agent.model}")

        total_tools = len(self.agent.tools)
        mcp_tools_count = len(self.agent.mcp_tools) if self.agent.mcp_tools else 0
        if mcp_tools_count > 0:
            print(f"🛠️  Tools: {total_tools} built-in + {mcp_tools_count} MCP tools")
        else:
            print(f"🛠️  Tools: {total_tools} loaded")

        status_indicators = [
            f"{'🟢' if verbose_mode else '🔇'} Verbose: {'On' if verbose_mode else 'Off'}",
            f"{'🌊' if stream else '📄'} Stream: {'On' if stream else 'Off'}",
            f"{'🧠' if memory else '🚫'} Memory: {'On' if memory else 'Off'}",
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
        print("─" * 60)

    def _show_help(self):
        print("\n╭─ 📋 Commands ─────────────────────────────────────────────╮")
        print("│ exit, quit, bye    Exit the chat session                  │")
        print("│ clear              Clear the agent message stream         │")
        print("│ stream on/off      Toggle streaming response mode         │")
        print("│ memory on/off      Toggle memory storage mode             │")
        print("│ help               Show this help message                 │")
        print("╰───────────────────────────────────────────────────────────╯")

        print("\n╭─ 🔧 Built-in Tools ───────────────────────────────────────╮")
        if self.agent.tools:
            for i, tool_name in enumerate(self.agent.tools.keys(), 1):
                print(f"│ {i:2d}. {tool_name:<50}    │")
        else:
            print("│ No built-in tools available                              │")
        print("╰───────────────────────────────────────────────────────────╯")

        if self.agent.mcp_tools:
            print("\n╭─ 🌐 MCP Tools ────────────────────────────────────────────╮")
            for i, tool_name in enumerate(self.agent.mcp_tools.keys(), 1):
                print(f"│ {i:2d}. {tool_name:<50} │")
            print("╰───────────────────────────────────────────────────────────╯")


def create_default_config_file(config_path: str = "config/agent.yaml"):
    """Create a default configuration file and toolkit directory structure."""
    config_dir = os.path.dirname(config_path)
    if config_dir and not os.path.exists(config_dir):
        os.makedirs(config_dir)

    default_config_yaml = """agent:
  name: Agent
  system_prompt: |
    You are a helpful assistant. Your task is to assist users
    with their queries and tasks.
  model: gpt-4o-mini
  capabilities:
    tools:
      - web_search
      - calculate_square
    mcp_servers:
      - http://localhost:8001/mcp/
  storage_mode: local

server:
  host: 0.0.0.0
  port: 8010
"""

    with open(config_path, "w", encoding="utf-8") as f:
        f.write(default_config_yaml)

    toolkit_dir = "my_toolkit"
    if not os.path.exists(toolkit_dir):
        os.makedirs(toolkit_dir)

    init_content = """from .tools import *

TOOLKIT_REGISTRY = {
    "calculate_square": calculate_square,
    "fetch_weather": fetch_weather
}
"""

    with open(os.path.join(toolkit_dir, "__init__.py"), "w", encoding="utf-8") as f:
        f.write(init_content)

    tools_content = """import asyncio
from xagent.utils.tool_decorator import function_tool

@function_tool()
def calculate_square(n: int) -> int:
    \"\"\"Calculate the square of a number.\"\"\"
    return n * n

@function_tool()
async def fetch_weather(city: str) -> str:
    \"\"\"Fetch weather data from an API.\"\"\"
    await asyncio.sleep(0.5)
    return f"Weather in {city}: 22°C, Sunny"
"""

    with open(os.path.join(toolkit_dir, "tools.py"), "w", encoding="utf-8") as f:
        f.write(tools_content)

    print("╭─────────────────────────────────────────────────────────╮")
    print("│ ✅ Configuration and Toolkit Created Successfully!      │")
    print("╰─────────────────────────────────────────────────────────╯")
    print(f"📁 Config: {config_path}")
    print(f"🛠️  Toolkit: {toolkit_dir}/")


def main():
    parser = argparse.ArgumentParser(description="xAgent CLI - Interactive chat agent")
    parser.add_argument("--config", default=None, help="Config file path (if not specified, uses default configuration)")
    parser.add_argument("--toolkit_path", default=None, help="Toolkit directory path (if not specified, no additional tools will be loaded)")
    parser.add_argument("--user_id", help="Speaker identifier for the chat")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    parser.add_argument("--ask", metavar="MESSAGE", help="Ask a single question instead of starting interactive chat")
    parser.add_argument("--init", action="store_true", help="Create default configuration file and exit")

    args = parser.parse_args()

    if os.path.exists(args.env):
        load_dotenv(args.env, override=True)
        if args.verbose:
            print(f"\n✅ Loaded .env file from: {args.env}\n")
    elif args.verbose:
        print(f"\n⚠️  .env file not found: {args.env}\n")

    try:
        if args.init:
            create_default_config_file("config/agent.yaml")
            return

        agent_cli = AgentCLI(
            config_path=args.config,
            toolkit_path=args.toolkit_path,
            verbose=args.verbose,
        )

        if args.ask:
            response = asyncio.run(
                agent_cli.chat_single(
                    message=args.ask,
                    user_id=args.user_id,
                )
            )
            print(response)
            return

        asyncio.run(
            agent_cli.chat_interactive(
                user_id=args.user_id,
            )
        )

    except Exception as exc:
        print(f"Failed to start CLI: {exc}")
        raise


if __name__ == "__main__":
    main()
