import os
import argparse
import asyncio
import uuid
import logging
from typing import Optional
from dotenv import load_dotenv

from .base import BaseAgentRunner


class AgentCLI(BaseAgentRunner):
    """CLI Agent for xAgent."""
    
    def __init__(self, config_path: Optional[str] = None, toolkit_path: Optional[str] = None, verbose: bool = False):
        """
        Initialize AgentCLI.
        
        Args:
            config_path: Path to configuration file (if None, uses default configuration)
            toolkit_path: Path to toolkit directory (if None, no additional tools will be loaded)
            verbose: Enable verbose logging output
        """
        # Configure logging based on verbose setting
        self.verbose = verbose
        
        # Always suppress Langfuse logs regardless of verbose mode
        logging.getLogger("langfuse").setLevel(logging.CRITICAL)
        
        if not verbose:
            # Suppress most logging except critical errors
            logging.getLogger().setLevel(logging.CRITICAL)
            logging.getLogger("xagent").setLevel(logging.CRITICAL)
            # Suppress all warnings when not in verbose mode
            import warnings
            warnings.filterwarnings("ignore")
        else:
            # Enable verbose logging
            logging.getLogger().setLevel(logging.INFO)
            logging.getLogger("xagent").setLevel(logging.INFO)
            # Keep Langfuse suppressed even in verbose mode
        
        # Initialize the base agent runner
        super().__init__(config_path, toolkit_path)
        
        # Store config_path for CLI-specific functionality
        self.config_path = config_path if config_path and os.path.isfile(config_path) else None
        
    async def chat_interactive(self, user_id: str = None, session_id: str = None, stream: bool = None, enable_memory: bool = False):
        """
        Start an interactive chat session.
        
        Args:
            user_id: User ID for the session
            session_id: Session ID for the chat
            stream: Enable streaming response (default: True, but False when verbose mode is enabled)
            enable_memory: Whether to enable memory storage and retrieval (default: False)
        """
        # If stream is not explicitly set, determine based on verbose mode
        if stream is None:
            # When verbose mode is enabled, default to non-streaming for better log readability
            stream = not (logging.getLogger().level <= logging.INFO)
        
        # Check if verbose mode is enabled by checking log level
        verbose_mode = logging.getLogger().level <= logging.INFO
        # Generate default IDs if not provided
        user_id = user_id or f"cli_user_{uuid.uuid4().hex[:8]}"
        session_id = session_id or f"cli_session_{uuid.uuid4().hex[:8]}"
        
        # Display welcome banner
        print("╭" + "─" * 58 + "╮")
        print("│" + " " * 18 + "🤖 Welcome to xAgent CLI!" + " " * 15 + "│")
        print("╰" + "─" * 58 + "╯")
        
        # Configuration information
        config_msg = f"📁 Config: {self.config_path}" if self.config_path else "📁 Config: Default configuration"
        print(f"\n{config_msg}")
        
        # Agent information in a clean format
        print(f"🤖 Agent: {self.agent.name}")
        print(f"🧠 Model: {self.agent.model}")
        
        # Tools information
        total_tools = len(self.agent.tools)
        mcp_tools_count = len(self.agent.mcp_tools) if self.agent.mcp_tools else 0
        if mcp_tools_count > 0:
            print(f"🛠️  Tools: {total_tools} built-in + {mcp_tools_count} MCP tools")
        else:
            print(f"🛠️  Tools: {total_tools} loaded")
        
        # Session information
        print(f"🔗 Session: {session_id}")
        
        # Status indicators
        status_indicators = []
        status_indicators.append(f"{'🟢' if verbose_mode else '🔇'} Verbose: {'On' if verbose_mode else 'Off'}")
        status_indicators.append(f"{'🌊' if stream else '📄'} Stream: {'On' if stream else 'Off'}")
        print(f"⚙️  Status: {' | '.join(status_indicators)}")
        
        # Performance tip
        if verbose_mode and stream:
            print("💡 Tip: Use 'stream off' for better log readability in verbose mode")
        
        # Quick start guide
        print(f"\n{'─' * 60}")
        print("🚀 Quick Start:")
        print("  • Type your message to chat with the agent")
        print("  • Use 'help' to see all available commands")
        print("  • Use 'exit', 'quit', or 'bye' to end session")
        print("  • Use 'clear' to reset conversation history")
        print("  • Use 'stream on/off' to toggle response streaming")
        print("─" * 60)
        
        while True:
            try:
                # Get user input
                user_input = input("\n👤 You: ").strip()
                
                # Handle special commands
                if user_input.lower() in ['exit', 'quit', 'bye']:
                    print("\n╭───────────────────────────────────────╮")
                    print("│  👋 Thank you for using xAgent CLI!   │")
                    print("│         See you next time! 🚀         │")
                    print("╰───────────────────────────────────────╯")
                    break
                elif user_input.lower() == 'clear':
                    await self.message_storage.clear_history(user_id, session_id)
                    print("🧹 ✨ Conversation history cleared. Fresh start!")
                    continue
                elif user_input.lower().startswith('stream '):
                    # Handle stream toggle command
                    stream_cmd = user_input.lower().split()
                    if len(stream_cmd) == 2:
                        if stream_cmd[1] == 'on':
                            stream = True
                            print("🌊 ✨ Streaming mode enabled.")
                        elif stream_cmd[1] == 'off':
                            stream = False
                            print("📄 ✨ Streaming mode disabled.")
                        else:
                            print("⚠️  Usage: stream on/off")
                    else:
                        print("⚠️  Usage: stream on/off")
                    continue
                elif user_input.lower() == 'help':
                    self._show_help()
                    continue
                elif not user_input:
                    print("💭 Please enter a message to chat with the agent.")
                    continue
                
                # Process the message
                if stream:
                    # Handle streaming response
                    response_generator = await self.agent(
                        user_message=user_input,
                        user_id=user_id,
                        session_id=session_id,
                        stream=True,
                        enable_memory=enable_memory
                    )
                    
                    # Check if response is a generator (streaming) or a string
                    if hasattr(response_generator, '__aiter__'):
                        print("🤖 Agent: ", end="", flush=True)
                        chunk_count = 0
                        async for chunk in response_generator:
                            if chunk:
                                print(chunk, end="", flush=True)
                                chunk_count += 1
                        print()  # Add newline after streaming is complete
                        if chunk_count == 0:
                            print("   (No response received)")
                    else:
                        # Fallback for non-streaming response
                        print("🤖 Agent: " + str(response_generator))
                else:
                    # Handle non-streaming response
                    print("🤖 Agent: ", end="", flush=True)
                    response = await self.agent(
                        user_message=user_input,
                        user_id=user_id,
                        session_id=session_id,
                        stream=False,
                        enable_memory=enable_memory
                    )
                    print(str(response))
                
            except KeyboardInterrupt:
                print("\n\n╭─────────────────────────────────────╮")
                print("│  👋 Session interrupted by user    │")
                print("│      Thank you for using xAgent!   │")
                print("╰─────────────────────────────────────╯")
                break
            except Exception as e:
                print(f"\n❌ Oops! An error occurred: {e}")
                if verbose_mode:
                    import traceback
                    print("🔍 Debug trace:")
                    traceback.print_exc()
    
    async def chat_single(self, message: str, user_id: str = None, session_id: str = None, enable_memory: bool = False):
        """
        Process a single message and return the response.
        
        Args:
            message: The message to process
            user_id: User ID for the session
            session_id: Session ID for the chat
            enable_memory: Whether to enable memory storage and retrieval (default: False)
            
        Returns:
            Agent response string
        """
        # Generate default IDs if not provided
        user_id = user_id or f"cli_user_{uuid.uuid4().hex[:8]}"
        session_id = session_id or f"cli_session_{uuid.uuid4().hex[:8]}"
        
        response = await self.agent(
            user_message=message,
            user_id=user_id,
            session_id=session_id,
            stream=False,
            enable_memory=enable_memory
        )
        
        return response
    
    def _show_help(self):
        """Show help information."""
        print("\n╭─ 📋 Commands ─────────────────────────────────────────────╮")
        print("│ exit, quit, bye    Exit the chat session                  │")
        print("│ clear              Clear conversation history             │")
        print("│ stream on/off      Toggle streaming response mode         │")
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
    """
    Create a default configuration file and toolkit directory structure.
    
    Args:
        config_path: Path where to create the config file
    """
    # Create directory if it doesn't exist
    config_dir = os.path.dirname(config_path)
    if config_dir and not os.path.exists(config_dir):
        os.makedirs(config_dir)
    
    # Default configuration written directly as YAML string to preserve field order
    default_config_yaml = """agent:
  name: Agent
  system_prompt: |
    You are a helpful assistant. Your task is to assist users
    with their queries and tasks.
  model: gpt-4o-mini
  capabilities:
    tools:
      - web_search
      - calculate_square # Custom tool
    mcp_servers:
      - http://localhost:8001/mcp/  # Example MCP server
  message_storage: local # support local and redis
  memory_storage: local # support local and upstash

server:
  host: 0.0.0.0
  port: 8010
"""
    
    with open(config_path, 'w', encoding='utf-8') as f:
        f.write(default_config_yaml)
    
    # Create default toolkit directory
    toolkit_dir = "my_toolkit"
    if not os.path.exists(toolkit_dir):
        os.makedirs(toolkit_dir)
    
    # Create __init__.py file
    init_content = """from .tools import *

TOOLKIT_REGISTRY = {
    "calculate_square": calculate_square,
    "fetch_weather": fetch_weather
}
"""
    
    with open(os.path.join(toolkit_dir, "__init__.py"), 'w', encoding='utf-8') as f:
        f.write(init_content)
    
    # Create tools.py file
    tools_content = """import asyncio
from xagent.utils.tool_decorator import function_tool

@function_tool()
def calculate_square(n: int) -> int:
    \"\"\"Calculate the square of a number.\"\"\"
    return n * n

@function_tool()
async def fetch_weather(city: str) -> str:
    \"\"\"Fetch weather data from an API.\"\"\"
    # Simulate API call
    await asyncio.sleep(0.5)
    return f"Weather in {city}: 22°C, Sunny"
"""
    
    with open(os.path.join(toolkit_dir, "tools.py"), 'w', encoding='utf-8') as f:
        f.write(tools_content)
    
    print("╭─────────────────────────────────────────────────────────╮")
    print("│ ✅ Configuration and Toolkit Created Successfully!      │")
    print("╰─────────────────────────────────────────────────────────╯")
    print(f"📁 Config: {config_path}")
    print(f"🛠️  Toolkit: {toolkit_dir}/")
    print("📝 Next steps:")
    print("  • Edit the configuration file to customize your agent")
    print(f"  • Use 'xagent-cli --config {config_path} --toolkit_path {toolkit_dir}' to load them")
    print("  • Add more tools to my_toolkit/tools.py and update TOOLKIT_REGISTRY")
    print("  • See documentation for all available options")


def main():
    """Main entry point for xagent-cli command."""
    parser = argparse.ArgumentParser(description="xAgent CLI - Interactive chat agent")
    
    # Main command arguments (no subcommands)
    parser.add_argument("--config", default=None, help="Config file path (if not specified, uses default configuration)")
    parser.add_argument("--toolkit_path", default=None, help="Toolkit directory path (if not specified, no additional tools will be loaded)")
    parser.add_argument("--user_id", help="User ID for the session")
    parser.add_argument("--session_id", help="Session ID for the chat")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    parser.add_argument("--enable_memory", action="store_true", default=False, help="Enable memory storage and retrieval (default: False)")
    
    # Special commands as optional arguments
    parser.add_argument("--ask", metavar="MESSAGE", help="Ask a single question instead of starting interactive chat")
    parser.add_argument("--init", action="store_true", help="Create default configuration file and exit")
    
    # Parse arguments
    args = parser.parse_args()
    
    # Load .env file (default: .env in current directory)
    if os.path.exists(args.env):
        load_dotenv(args.env, override=True)
        if args.verbose:
            print(f"\n✅ Loaded .env file from: {args.env}\n")
    else:
        if args.verbose:
            print(f"\n⚠️  .env file not found: {args.env}\n")
    
    try:
        # Handle init command
        if args.init:
            create_default_config_file("config/agent.yaml")
            return
        
        # Handle single question
        if args.ask:
            agent_cli = AgentCLI(
                config_path=args.config,
                toolkit_path=args.toolkit_path,
                verbose=args.verbose
            )
            response = asyncio.run(agent_cli.chat_single(
                message=args.ask,
                user_id=args.user_id,
                session_id=args.session_id,
                enable_memory=args.enable_memory
            ))
            print(response)
            return
        
        # Default behavior: start interactive chat
        agent_cli = AgentCLI(
            config_path=args.config,
            toolkit_path=args.toolkit_path,
            verbose=args.verbose
        )
        
        # Start interactive chat
        asyncio.run(agent_cli.chat_interactive(
            user_id=args.user_id,
            session_id=args.session_id,
            enable_memory=args.enable_memory
        ))
            
    except Exception as e:
        print(f"Failed to start CLI: {e}")
        raise


if __name__ == "__main__":
    main()
