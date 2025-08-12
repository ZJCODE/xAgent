import os
import yaml
import argparse
import asyncio
import uuid
import logging
from typing import Optional, Dict, Any
from dotenv import load_dotenv
import importlib.util
import sys

from ..core.agent import Agent
from ..core.session import Session
from ..db.message import MessageDB
from ..tools import TOOL_REGISTRY


class CLIAgent:
    """CLI Agent for xAgent."""
    
    def __init__(self, config_path: Optional[str] = None, toolkit_path: Optional[str] = None, verbose: bool = False):
        """
        Initialize CLIAgent.
        
        Args:
            config_path: Path to configuration file (if None, uses default configuration)
            toolkit_path: Path to toolkit directory (if None, no additional tools will be loaded)
            verbose: Enable verbose logging output
        """
        # Configure logging based on verbose setting
        if not verbose:
            # Suppress most logging except critical errors
            logging.getLogger().setLevel(logging.CRITICAL)
            logging.getLogger("xagent").setLevel(logging.CRITICAL)
        
        # Load environment variables
        load_dotenv(override=True)
        
        # Persist toolkit path for dynamic loading
        self.toolkit_path = toolkit_path
        
        # Load configuration
        self.config = self._load_config(config_path)
        
        # Initialize components
        self.agent = self._initialize_agent()
        self.message_db = self._initialize_message_db()
        
    def _load_config(self, cfg_path: Optional[str]) -> Dict[str, Any]:
        """
        Load YAML configuration file.
        
        Args:
            cfg_path: Path to config file (if None, uses default configuration)
            
        Returns:
            Configuration dictionary
        """
        # If no config path provided, use default configuration
        if cfg_path is None:
            self.config_path = None
            return self._get_default_config()
        
        # Check if the specified config file exists
        if os.path.isfile(cfg_path):
            self.config_path = cfg_path
            with open(cfg_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
        else:
            # Use default configuration if file doesn't exist
            self.config_path = None
            return self._get_default_config()
    
    def _get_default_config(self) -> Dict[str, Any]:
        """
        Return default configuration when no config file is found.
        
        Returns:
            Default configuration dictionary
        """
        return {
            "agent": {
                "name": "Agent",
                "system_prompt": "You are a helpful assistant. Your task is to assist users with their queries and tasks.",
                "model": "gpt-4o-mini",
                "tools": ["web_search"],  # No default tools, can be added via toolkit or config
                "use_local_session": True
            },
            "server": {
                "host": "0.0.0.0",
                "port": 8010
            }
        }
    
    def _load_toolkit_registry(self, toolkit_path: Optional[str]) -> Dict[str, Any]:
        """Dynamically load TOOLKIT_REGISTRY from a toolkit directory.
        Only a directory path is supported; do not pass __init__.py.
        Returns empty dict if unavailable or on error.
        """
        if not toolkit_path:
            return {}
        try:
            # Resolve relative paths against this file's directory
            def resolve_path(p: str) -> str:
                if os.path.isabs(p):
                    return p
                if os.path.exists(p):
                    return p
                base = os.path.dirname(os.path.abspath(__file__))
                candidate = os.path.join(base, p)
                return candidate

            tp = resolve_path(toolkit_path)

            # Require a directory
            if not os.path.isdir(tp):
                return {}

            init_path = os.path.join(tp, "__init__.py")
            if not os.path.isfile(init_path):
                return {}

            # Mark as a package so relative imports inside __init__.py work
            spec = importlib.util.spec_from_file_location(
                "xagent_dynamic_toolkit",
                init_path,
                submodule_search_locations=[tp],
            )
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                sys.modules["xagent_dynamic_toolkit"] = module
                spec.loader.exec_module(module)  # type: ignore[attr-defined]
                registry = getattr(module, "TOOLKIT_REGISTRY", {})
                if isinstance(registry, dict):
                    return registry
        except Exception as e:
            print(f"Warning: failed to load TOOLKIT_REGISTRY from {toolkit_path}: {e}")
        return {}
    
    def _initialize_agent(self) -> Agent:
        """Initialize the agent with tools and configuration."""
        agent_cfg = self.config.get("agent", {})
        
        # Load tools from built-in registry and optional toolkit registry
        tool_names = agent_cfg.get("tools", [])
        toolkit_registry = self._load_toolkit_registry(self.toolkit_path)
        combined_registry: Dict[str, Any] = {**TOOL_REGISTRY, **toolkit_registry}
        tools = [combined_registry[name] for name in tool_names if name in combined_registry]
        
        return Agent(
            name=agent_cfg.get("name"),
            system_prompt=agent_cfg.get("system_prompt"),
            model=agent_cfg.get("model"),
            tools=tools,
            mcp_servers=agent_cfg.get("mcp_servers"),
        )
    
    def _initialize_message_db(self) -> Optional[MessageDB]:
        """Initialize message database based on configuration."""
        agent_cfg = self.config.get("agent", {})
        use_local_session = agent_cfg.get("use_local_session", True)
        return None if use_local_session else MessageDB()
    
    async def chat_interactive(self, user_id: str = None, session_id: str = None, stream: bool = True):
        """
        Start an interactive chat session.
        
        Args:
            user_id: User ID for the session
            session_id: Session ID for the chat
            stream: Enable streaming response (default: True)
        """
        # Generate default IDs if not provided
        user_id = user_id or f"cli_user_{uuid.uuid4().hex[:8]}"
        session_id = session_id or f"cli_session_{uuid.uuid4().hex[:8]}"
        
        session = Session(
            user_id=user_id,
            session_id=session_id,
            message_db=self.message_db
        )
        
        print(f"🤖 Welcome to xAgent CLI!")
        config_msg = f"Loading agent configuration from {self.config_path}" if self.config_path else "Using default configuration"
        print(config_msg)
        print(f"Agent: {self.agent.name}")
        print(f"Model: {self.agent.model}")
        print(f"Tools: {len(self.agent.tools)} loaded")
        print(f"Session: {session_id}")
        print(f"Streaming: {'Enabled' if stream else 'Disabled'}")
        print("Type 'exit', 'quit', or 'bye' to end the session.")
        print("Type 'clear' to clear the session history.")
        print("Type 'stream on/off' to toggle streaming mode.")
        print("Type 'help' for available commands.")
        print("-" * 50)
        
        while True:
            try:
                # Get user input
                user_input = input("\n👤 You: ").strip()
                
                # Handle special commands
                if user_input.lower() in ['exit', 'quit', 'bye']:
                    print("👋 Goodbye!")
                    break
                elif user_input.lower() == 'clear':
                    await session.clear_session()
                    print("🧹 Session history cleared.")
                    continue
                elif user_input.lower().startswith('stream '):
                    # Handle stream toggle command
                    stream_cmd = user_input.lower().split()
                    if len(stream_cmd) == 2:
                        if stream_cmd[1] == 'on':
                            stream = True
                            print("🌊 Streaming mode enabled.")
                        elif stream_cmd[1] == 'off':
                            stream = False
                            print("📄 Streaming mode disabled.")
                        else:
                            print("⚠️  Usage: stream on/off")
                    else:
                        print("⚠️  Usage: stream on/off")
                    continue
                elif user_input.lower() == 'help':
                    self._show_help()
                    continue
                elif not user_input:
                    continue
                
                # Process the message
                print("🤖 Agent: ", end="", flush=True)
                
                if stream:
                    # Handle streaming response
                    response_generator = await self.agent(
                        user_message=user_input,
                        session=session,
                        stream=True
                    )
                    
                    # Check if response is a generator (streaming) or a string
                    if hasattr(response_generator, '__aiter__'):
                        async for chunk in response_generator:
                            if chunk:
                                print(chunk, end="", flush=True)
                        print()  # Add newline after streaming is complete
                    else:
                        # Fallback for non-streaming response
                        print(response_generator)
                else:
                    # Handle non-streaming response
                    response = await self.agent(
                        user_message=user_input,
                        session=session,
                        stream=False
                    )
                    print(response)
                
            except KeyboardInterrupt:
                print("\n\n👋 Session interrupted. Goodbye!")
                break
            except Exception as e:
                print(f"\n❌ Error: {e}")
    
    async def chat_single(self, message: str, user_id: str = None, session_id: str = None):
        """
        Process a single message and return the response.
        
        Args:
            message: The message to process
            user_id: User ID for the session
            session_id: Session ID for the chat
            
        Returns:
            Agent response string
        """
        # Generate default IDs if not provided
        user_id = user_id or f"cli_user_{uuid.uuid4().hex[:8]}"
        session_id = session_id or f"cli_session_{uuid.uuid4().hex[:8]}"
        
        session = Session(
            user_id=user_id,
            session_id=session_id,
            message_db=self.message_db
        )
        
        response = await self.agent(
            user_message=message,
            session=session,
            stream=False
        )
        
        return response
    
    def _show_help(self):
        """Show help information."""
        print("\n📋 Available commands:")
        print("  exit, quit, bye  - Exit the chat session")
        print("  clear           - Clear session history")
        print("  stream on/off   - Toggle streaming mode")
        print("  help            - Show this help message")
        print("\n🔧 Available tools:")
        for tool_name in self.agent.tools.keys():
            print(f"  - {tool_name}")
        if self.agent.mcp_tools:
            print("\n🌐 MCP tools:")
            for tool_name in self.agent.mcp_tools.keys():
                print(f"  - {tool_name}")
    
    def create_default_config(self, config_path: str = "config/agent.yaml"):
        """
        Create a default configuration file.
        
        Args:
            config_path: Path where to create the config file
        """
        # Create directory if it doesn't exist
        config_dir = os.path.dirname(config_path)
        if config_dir and not os.path.exists(config_dir):
            os.makedirs(config_dir)
        
        default_config = self._get_default_config()
        
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(default_config, f, default_flow_style=False, allow_unicode=True)
        
        print(f"✅ Default configuration created at: {config_path}")
        print("You can edit this file to customize your agent settings.")


def create_default_config_file(config_path: str = "config/agent.yaml"):
    """
    Create a default configuration file.
    
    Args:
        config_path: Path where to create the config file
    """
    # Create directory if it doesn't exist
    config_dir = os.path.dirname(config_path)
    if config_dir and not os.path.exists(config_dir):
        os.makedirs(config_dir)
    
    default_config = {
        "agent": {
            "name": "Agent",
            "system_prompt": "You are a helpful assistant. Your task is to assist users with their queries and tasks.",
            "model": "gpt-4o-mini",
            "tools": [],
            "use_local_session": True
        },
        "server": {
            "host": "0.0.0.0",
            "port": 8010
        }
    }
    
    with open(config_path, 'w', encoding='utf-8') as f:
        yaml.dump(default_config, f, default_flow_style=False, allow_unicode=True)
    
    print(f"✅ Default configuration created at: {config_path}")
    print("You can edit this file to customize your agent settings.")


def main():
    """Main entry point for xagent-cli command."""
    parser = argparse.ArgumentParser(description="xAgent CLI")
    
    # Subcommands
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # Interactive chat command
    chat_parser = subparsers.add_parser("chat", help="Start interactive chat session")
    chat_parser.add_argument("--config", default=None, help="Config file path (if not specified, uses default configuration)")
    chat_parser.add_argument("--toolkit_path", default=None, help="Toolkit directory path (if not specified, no additional tools will be loaded)")
    chat_parser.add_argument("--user_id", help="User ID for the session")
    chat_parser.add_argument("--session_id", help="Session ID for the chat")
    chat_parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    chat_parser.add_argument("--no-stream", action="store_true", help="Disable streaming response (default: streaming enabled)")
    
    # Single message command
    single_parser = subparsers.add_parser("ask", help="Ask a single question")
    single_parser.add_argument("message", help="The message to send to the agent")
    single_parser.add_argument("--config", default=None, help="Config file path (if not specified, uses default configuration)")
    single_parser.add_argument("--toolkit_path", default=None, help="Toolkit directory path (if not specified, no additional tools will be loaded)")
    single_parser.add_argument("--user_id", help="User ID for the session")
    single_parser.add_argument("--session_id", help="Session ID for the chat")
    single_parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    
    # Init command to create default config
    init_parser = subparsers.add_parser("init", help="Create default configuration file")
    init_parser.add_argument("--config", default="config/agent.yaml", help="Config file path to create")
    
    # Parse arguments
    args = parser.parse_args()
    
    # If no command specified, default to interactive chat
    if not args.command:
        args.command = "chat"
        args.config = None  # Use default configuration when no command is specified
        args.toolkit_path = None  # No toolkit by default
        args.user_id = None
        args.session_id = None
        args.verbose = False
        args.no_stream = False
    
    try:
        if args.command == "init":
            create_default_config_file(args.config)
            return
            
        cli_agent = CLIAgent(
            config_path=args.config, 
            toolkit_path=args.toolkit_path,
            verbose=getattr(args, 'verbose', False)
        )
        
        if args.command == "chat":
            asyncio.run(cli_agent.chat_interactive(
                user_id=args.user_id,
                session_id=args.session_id,
                stream=not getattr(args, 'no_stream', False)
            ))
        elif args.command == "ask":
            response = asyncio.run(cli_agent.chat_single(
                message=args.message,
                user_id=args.user_id,
                session_id=args.session_id
            ))
            print(response)
        else:
            parser.print_help()
            
    except Exception as e:
        print(f"Failed to start CLI: {e}")
        raise


if __name__ == "__main__":
    main()
