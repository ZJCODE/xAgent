import os
import yaml
import importlib.util
import sys
from typing import Optional, Dict, Any, Type, List
from dotenv import load_dotenv
from pydantic import BaseModel, create_model

from ..core.agent import Agent
from ..db.message import MessageDB
from ..tools import TOOL_REGISTRY


class BaseAgentRunner:
    """Base class for agent runners with common configuration and initialization logic."""
    
    def __init__(self, config_path: Optional[str] = None, toolkit_path: Optional[str] = None):
        """
        Initialize BaseAgentRunner.
        
        Args:
            config_path: Path to configuration file (if None, uses default configuration)
            toolkit_path: Path to toolkit directory (if None, no additional tools will be loaded)
        """
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
            return self._get_default_config()
        
        # Check if the specified config file exists
        if os.path.isfile(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
        else:
            # Use default configuration if file doesn't exist
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
                "capabilities": {
                    "tools": ["web_search"],  # Default tools
                    "mcp_servers": []  # Default MCP servers
                },
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
    
    def _create_output_model_from_schema(self, output_schema: Optional[Dict[str, Any]]) -> Optional[Type[BaseModel]]:
        """
        Create a dynamic Pydantic BaseModel from YAML output_schema configuration.
        
        Args:
            output_schema: Dictionary containing class_name and fields configuration
            
        Returns:
            Dynamic Pydantic BaseModel class or None if no schema provided
        """
        if not output_schema:
            return None
        
        try:
            class_name = output_schema.get("class_name", "DynamicModel")
            fields_config = output_schema.get("fields", {})
            
            if not fields_config:
                return None
            
            # Build field definitions for create_model
            field_definitions = {}
            for field_name, field_config in fields_config.items():
                field_type = field_config.get("type", "str")
                field_description = field_config.get("description", "")
                
                # Map string type names to actual Python types
                type_mapping = {
                    "str": str,
                    "int": int,
                    "float": float,
                    "bool": bool,
                    "list": list,
                    "dict": dict,
                }
                
                python_type = type_mapping.get(field_type, str)
                
                # Handle list types with items specification
                if field_type == "list" and "items" in field_config:
                    items_type = field_config["items"]
                    items_python_type = type_mapping.get(items_type, str)
                    # Create List[items_type] annotation
                    python_type = List[items_python_type]
                
                # Create field definition (type, default_value, Field(...))
                from pydantic import Field
                field_definitions[field_name] = (python_type, Field(description=field_description))
            
            # Create the dynamic model
            dynamic_model = create_model(class_name, **field_definitions)
            return dynamic_model
            
        except Exception as e:
            print(f"Warning: failed to create output model from schema: {e}")
            return None
    
    def _initialize_agent(self) -> Agent:
        """Initialize the agent with tools and configuration."""
        agent_cfg = self.config.get("agent", {})
        
        # Load tools from built-in registry and optional toolkit registry
        capabilities = agent_cfg.get("capabilities", {})
        tool_names = capabilities.get("tools", [])
        mcp_servers = capabilities.get("mcp_servers", [])
        
        # Support legacy format for backward compatibility
        if "tools" in agent_cfg and "tools" not in capabilities:
            tool_names = agent_cfg.get("tools", [])
        if "mcp_servers" in agent_cfg and "mcp_servers" not in capabilities:
            mcp_servers = agent_cfg.get("mcp_servers", [])
        
        toolkit_registry = self._load_toolkit_registry(self.toolkit_path)
        combined_registry: Dict[str, Any] = {**TOOL_REGISTRY, **toolkit_registry}
        tools = [combined_registry[name] for name in tool_names if name in combined_registry]
        
        # Process sub_agents configuration
        sub_agents = None
        if "sub_agents" in agent_cfg:
            sub_agents = []
            for agent_config in agent_cfg["sub_agents"]:
                if isinstance(agent_config, dict):
                    # Convert dict to tuple format
                    sub_agents.append((
                        agent_config.get("name", ""),
                        agent_config.get("description", ""),
                        agent_config.get("server_url", "")
                    ))
                elif isinstance(agent_config, (list, tuple)) and len(agent_config) == 3:
                    # Already in tuple format
                    sub_agents.append(tuple(agent_config))
        
        # Create output model from schema if provided
        output_type = None
        if "output_schema" in agent_cfg:
            output_type = self._create_output_model_from_schema(agent_cfg["output_schema"])

        return Agent(
            name=agent_cfg.get("name"),
            system_prompt=agent_cfg.get("system_prompt"),
            model=agent_cfg.get("model"),
            tools=tools,
            mcp_servers=mcp_servers,
            sub_agents=sub_agents,
            output_type=output_type,
        )
    
    def _initialize_message_db(self) -> Optional[MessageDB]:
        """Initialize message database based on configuration."""
        agent_cfg = self.config.get("agent", {})
        use_local_session = agent_cfg.get("use_local_session", True)
        return None if use_local_session else MessageDB()
