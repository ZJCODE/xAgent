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
import json

from xagent.core.agent import Agent
from xagent.core.session import Session
from xagent.db.message import MessageDB
from xagent.tools import TOOL_REGISTRY


class CLIAgent:
    """CLI Agent for xAgent."""
    
    def __init__(self, config_path: str = "config/agent.yaml", toolkit_path: str = "toolkit", verbose: bool = False):
        """
        Initialize CLIAgent.
        
        Args:
            config_path: Path to configuration file
            toolkit_path: Path to toolkit directory
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
        
    def _load_config(self, cfg_path: str) -> Dict[str, Any]:
        """
        Load YAML configuration file.
        
        Args:
            cfg_path: Path to config file
            
        Returns:
            Configuration dictionary
            
        Raises:
            FileNotFoundError: If config file not found
        """
        if not os.path.isfile(cfg_path):
            # Support relative path lookup from project root first
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            root_path = os.path.join(project_root, cfg_path)
            if os.path.isfile(root_path):
                cfg_path = root_path
            else:
                # Fallback to relative path from this file
                base = os.path.dirname(os.path.abspath(__file__))
                abs_path = os.path.join(base, cfg_path)
                if os.path.isfile(abs_path):
                    cfg_path = abs_path
                else:
                    raise FileNotFoundError(f"Cannot find config file at {cfg_path}, {root_path}, or {abs_path}")
            
        with open(cfg_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    
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
    
    async def chat_interactive(self, user_id: str = None, session_id: str = None):
        """
        Start an interactive chat session.
        
        Args:
            user_id: User ID for the session
            session_id: Session ID for the chat
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
        print(f"Agent: {self.agent.name}")
        print(f"Model: {self.agent.model}")
        print(f"Tools: {len(self.agent.tools)} loaded")
        print(f"Session: {session_id}")
        print("Type 'exit', 'quit', or 'bye' to end the session.")
        print("Type 'clear' to clear the session history.")
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
                elif user_input.lower() == 'help':
                    self._show_help()
                    continue
                elif not user_input:
                    continue
                
                # Process the message
                print("🤖 Agent: ", end="", flush=True)
                response = await self.agent(
                    user_message=user_input,
                    session=session
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
            Agent response
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
            session=session
        )
        
        return response
    
    def _show_help(self):
        """Show help information."""
        print("\n📋 Available commands:")
        print("  exit, quit, bye  - Exit the chat session")
        print("  clear           - Clear session history")
        print("  help            - Show this help message")
        print("\n🔧 Available tools:")
        for tool_name in self.agent.tools.keys():
            print(f"  - {tool_name}")
        if self.agent.mcp_tools:
            print("\n🌐 MCP tools:")
            for tool_name in self.agent.mcp_tools.keys():
                print(f"  - {tool_name}")


def main():
    """Main entry point for xagent-cli command."""
    parser = argparse.ArgumentParser(description="xAgent CLI")
    
    # Subcommands
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # Interactive chat command
    chat_parser = subparsers.add_parser("chat", help="Start interactive chat session")
    chat_parser.add_argument("--config", default="config/agent.yaml", help="Config file path")
    chat_parser.add_argument("--toolkit_path", default="toolkit", help="Toolkit directory path")
    chat_parser.add_argument("--user_id", help="User ID for the session")
    chat_parser.add_argument("--session_id", help="Session ID for the chat")
    chat_parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    
    # Single message command
    single_parser = subparsers.add_parser("ask", help="Ask a single question")
    single_parser.add_argument("message", help="The message to send to the agent")
    single_parser.add_argument("--config", default="config/agent.yaml", help="Config file path")
    single_parser.add_argument("--toolkit_path", default="toolkit", help="Toolkit directory path")
    single_parser.add_argument("--user_id", help="User ID for the session")
    single_parser.add_argument("--session_id", help="Session ID for the chat")
    single_parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    
    # Parse arguments
    args = parser.parse_args()
    
    # If no command specified, default to interactive chat
    if not args.command:
        args.command = "chat"
        args.config = "config/agent.yaml"
        args.toolkit_path = "toolkit"
        args.user_id = None
        args.session_id = None
        args.verbose = False
    
    try:
        cli_agent = CLIAgent(
            config_path=args.config, 
            toolkit_path=args.toolkit_path,
            verbose=getattr(args, 'verbose', False)
        )
        
        if args.command == "chat":
            asyncio.run(cli_agent.chat_interactive(
                user_id=args.user_id,
                session_id=args.session_id
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
