# Standard library imports
import importlib.util
import os
import sys
from typing import Any, Dict, List, Optional, Type

# Third-party imports
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, create_model

# Local imports
from ..core.agent import Agent
from ..components import MessageStorageLocal,MessageStorageRedis,MessageStorageBase
from ..components import MemoryStorageBase,MemoryStorageLocal,MemoryStorageUpstash
from ..tools import TOOL_REGISTRY


class BaseAgentConfig:
    """Configuration constants for BaseAgentRunner."""
    
    DEFAULT_AGENT_NAME = "Agent"
    DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."
    DEFAULT_MODEL = "gpt-4o-mini"
    DEFAULT_MESSAGE_STORAGE = "local"
    DEFAULT_MEMORY_STORAGE = "local"
    DEFAULT_HOST = "0.0.0.0"
    DEFAULT_PORT = 8010

class BaseAgentRunner:
    """
    Base class for agent runners with common configuration and initialization logic.
    
    This class provides a standardized way to:
    - Load and validate agent configurations from YAML files
    - Initialize agents with tools, MCP servers, and sub-agents
    - Manage message databases and toolkit registries
    - Create dynamic Pydantic models from schema definitions
    
    Attributes:
        config: Loaded configuration dictionary
        agent: Initialized Agent instance
        message_storage: Optional MessageDB instance
        toolkit_path: Path to additional toolkit directory
    """
    
    def __init__(
        self, 
        config_path: Optional[str] = None, 
        toolkit_path: Optional[str] = None
    ):
        """
        Initialize BaseAgentRunner.
        
        Args:
            config_path: Path to configuration file. If None, uses default configuration
            toolkit_path: Path to toolkit directory. If None, no additional tools loaded
            
        Raises:
            yaml.YAMLError: If configuration file is invalid
            ImportError: If toolkit module cannot be loaded
        """
        # Load environment variables first
        load_dotenv(override=True)
        
        # Store paths for later use
        self.toolkit_path = toolkit_path
        
        # Load and validate configuration
        self.config = self._load_config(config_path)
        
        # Initialize components in dependency order
        self.message_storage = self._initialize_message_storage()
        self.memory_storage = self._initialize_memory_storage()
        self.agent = self._initialize_agent()
        
    def _load_config(self, cfg_path: Optional[str]) -> Dict[str, Any]:
        """
        Load YAML configuration file with error handling.
        
        Args:
            cfg_path: Path to config file. If None, uses default configuration
            
        Returns:
            Configuration dictionary
            
        Raises:
            yaml.YAMLError: If YAML file is malformed
            FileNotFoundError: If specified config file doesn't exist
        """
        if cfg_path is None:
            return self._get_default_config()
        
        if not os.path.isfile(cfg_path):
            raise FileNotFoundError(f"Configuration file not found: {cfg_path}")
        
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
                return self._validate_config(config)
        except yaml.YAMLError as e:
            raise yaml.YAMLError(f"Invalid YAML in config file {cfg_path}: {e}")
    
    def _validate_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validate and normalize configuration dictionary.
        
        Args:
            config: Raw configuration dictionary
            
        Returns:
            Validated and normalized configuration
        """
        if not isinstance(config, dict):
            raise ValueError("Configuration must be a dictionary")
        
        # Ensure required sections exist
        if "agent" not in config:
            config["agent"] = {}
        if "server" not in config:
            config["server"] = {}
        
        return config
    
    def _get_default_config(self) -> Dict[str, Any]:
        """
        Return default configuration when no config file is provided.
        
        Returns:
            Default configuration dictionary with sensible defaults
        """
        return {
            "agent": {
                "name": BaseAgentConfig.DEFAULT_AGENT_NAME,
                "system_prompt": BaseAgentConfig.DEFAULT_SYSTEM_PROMPT,
                "model": BaseAgentConfig.DEFAULT_MODEL,
                "capabilities": {
                    "tools": ["web_search"],  # Default tools
                    "mcp_servers": []  # Default MCP servers
                },
                "message_storage": BaseAgentConfig.DEFAULT_MESSAGE_STORAGE,
                "memory_storage": BaseAgentConfig.DEFAULT_MEMORY_STORAGE
            },
            "server": {
                "host": BaseAgentConfig.DEFAULT_HOST,
                "port": BaseAgentConfig.DEFAULT_PORT
            }
        }
    
    def _load_toolkit_registry(self, toolkit_path: Optional[str]) -> Dict[str, Any]:
        """
        Dynamically load TOOLKIT_REGISTRY from a toolkit directory.
        
        Args:
            toolkit_path: Path to toolkit directory (only directory paths supported)
            
        Returns:
            Dictionary containing loaded toolkit registry, empty if unavailable
            
        Note:
            Only directory paths are supported; do not pass __init__.py directly.
            Returns empty dict if loading fails or toolkit is unavailable.
        """
        if not toolkit_path:
            return {}
            
        try:
            resolved_path = self._resolve_toolkit_path(toolkit_path)
            
            if not self._is_valid_toolkit_directory(resolved_path):
                return {}
            
            return self._import_toolkit_module(resolved_path)
            
        except Exception as e:
            print(f"Warning: failed to load TOOLKIT_REGISTRY from {toolkit_path}: {e}")
            return {}
    
    def _resolve_toolkit_path(self, toolkit_path: str) -> str:
        """Resolve relative toolkit paths against this file's directory."""
        if os.path.isabs(toolkit_path):
            return toolkit_path
        if os.path.exists(toolkit_path):
            return toolkit_path
        
        base_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base_dir, toolkit_path)
    
    def _is_valid_toolkit_directory(self, path: str) -> bool:
        """Check if path is a valid toolkit directory with __init__.py."""
        return (
            os.path.isdir(path) and 
            os.path.isfile(os.path.join(path, "__init__.py"))
        )
    
    def _import_toolkit_module(self, toolkit_path: str) -> Dict[str, Any]:
        """Import toolkit module and extract TOOLKIT_REGISTRY."""

        DYNAMIC_TOOLKIT_MODULE_NAME = "xagent_dynamic_toolkit"

        init_path = os.path.join(toolkit_path, "__init__.py")

        # Create module spec with submodule search locations for relative imports
        spec = importlib.util.spec_from_file_location(
            DYNAMIC_TOOLKIT_MODULE_NAME,
            init_path,
            submodule_search_locations=[toolkit_path],
        )
        
        if not spec or not spec.loader:
            return {}
        
        # Load and execute module
        module = importlib.util.module_from_spec(spec)
        sys.modules[DYNAMIC_TOOLKIT_MODULE_NAME] = module
        spec.loader.exec_module(module)  # type: ignore[attr-defined]
        
        # Extract registry
        registry = getattr(module, "TOOLKIT_REGISTRY", {})
        return registry if isinstance(registry, dict) else {}
    
    def _create_output_model_from_schema(
        self, 
        output_schema: Optional[Dict[str, Any]]
    ) -> Optional[Type[BaseModel]]:
        """
        Create a dynamic Pydantic BaseModel from YAML output_schema configuration.
        
        Args:
            output_schema: Dictionary containing class_name and fields configuration
            
        Returns:
            Dynamic Pydantic BaseModel class or None if no schema provided
            
        Raises:
            ValueError: If schema format is invalid
        """
        if not output_schema:
            return None
        
        try:
            class_name = output_schema.get("class_name", "DynamicModel")
            fields_config = output_schema.get("fields", {})
            
            if not fields_config:
                return None
            
            field_definitions = self._build_field_definitions(fields_config)
            return create_model(class_name, **field_definitions)
            
        except Exception as e:
            print(f"Warning: failed to create output model from schema: {e}")
            return None
    
    def _build_field_definitions(self, fields_config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Build field definitions for create_model from fields configuration.
        
        Args:
            fields_config: Dictionary of field configurations
            
        Returns:
            Dictionary of field definitions suitable for create_model
        """
        field_definitions = {}
        
        for field_name, field_config in fields_config.items():
            field_type = field_config.get("type", "str")
            field_description = field_config.get("description", "")
            
            python_type = self._get_python_type(field_type, field_config)
            field_definitions[field_name] = (
                python_type, 
                Field(description=field_description)
            )
        
        return field_definitions
    
    def _get_python_type(self, field_type: str, field_config: Dict[str, Any]) -> Type:
        """
        Convert string type name to Python type, handling complex types.
        
        Args:
            field_type: String representation of the type
            field_config: Complete field configuration
            
        Returns:
            Python type for the field
        """

        # Type mappings for dynamic model creation
        TYPE_MAPPING = {
            "str": str,
            "int": int,
            "float": float,
            "bool": bool,
            "list": list,
            "dict": dict,
        }

        python_type = TYPE_MAPPING.get(field_type, str)

        # Handle list types with items specification
        if field_type == "list" and "items" in field_config:
            items_type = field_config["items"]
            items_python_type = TYPE_MAPPING.get(items_type, str)
            python_type = List[items_python_type]
        
        return python_type
    
    def _initialize_agent(self) -> Agent:
        """
        Initialize the agent with tools and configuration.
        
        Returns:
            Configured Agent instance
            
        Raises:
            KeyError: If required configuration is missing
            ImportError: If tool cannot be loaded
        """
        agent_cfg = self.config.get("agent", {})
        
        # Load tools and servers
        tools = self._load_agent_tools(agent_cfg)
        mcp_servers = self._get_mcp_servers(agent_cfg)
        sub_agents = self._process_sub_agents_config(agent_cfg)
        output_type = self._get_output_type(agent_cfg)

        return Agent(
            name=agent_cfg.get("name"),
            system_prompt=agent_cfg.get("system_prompt"),
            model=agent_cfg.get("model"),
            tools=tools,
            mcp_servers=mcp_servers,
            sub_agents=sub_agents,
            output_type=output_type,
            message_storage=self.message_storage,
            memory_storage=self.memory_storage,
        )
    
    def _load_agent_tools(self, agent_cfg: Dict[str, Any]) -> List[Any]:
        """Load tools from built-in registry and optional toolkit registry."""
        capabilities = agent_cfg.get("capabilities", {})
        tool_names = capabilities.get("tools", [])
        
        # Support legacy format for backward compatibility
        if "tools" in agent_cfg and "tools" not in capabilities:
            tool_names = agent_cfg.get("tools", [])
        
        # Combine registries
        toolkit_registry = self._load_toolkit_registry(self.toolkit_path)
        combined_registry: Dict[str, Any] = {**TOOL_REGISTRY, **toolkit_registry}
        
        # Load requested tools
        tools = []
        for name in tool_names:
            if name in combined_registry:
                tools.append(combined_registry[name])
            else:
                print(f"Warning: Tool '{name}' not found in registry")
        
        return tools
    
    def _get_mcp_servers(self, agent_cfg: Dict[str, Any]) -> List[str]:
        """Extract MCP servers configuration with backward compatibility."""
        capabilities = agent_cfg.get("capabilities", {})
        mcp_servers = capabilities.get("mcp_servers", [])
        
        # Support legacy format for backward compatibility
        if "mcp_servers" in agent_cfg and "mcp_servers" not in capabilities:
            mcp_servers = agent_cfg.get("mcp_servers", [])
        
        return mcp_servers
    
    def _process_sub_agents_config(
        self, 
        agent_cfg: Dict[str, Any]
    ) -> Optional[List[tuple[str, str, str]]]:
        """Process sub_agents configuration into standardized format."""
        if "sub_agents" not in agent_cfg:
            return None
        
        sub_agents = []
        for agent_config in agent_cfg["sub_agents"]:
            normalized_config = self._normalize_sub_agent_config(agent_config)
            if normalized_config:
                sub_agents.append(normalized_config)
        
        return sub_agents if sub_agents else None
    
    def _normalize_sub_agent_config(
        self, 
        agent_config: Any
    ) -> Optional[tuple[str, str, str]]:
        """Normalize sub-agent configuration to tuple format."""
        if isinstance(agent_config, dict):
            return (
                agent_config.get("name", ""),
                agent_config.get("description", ""),
                agent_config.get("server_url", "")
            )
        elif isinstance(agent_config, (list, tuple)) and len(agent_config) == 3:
            return tuple(agent_config)
        else:
            print(f"Warning: Invalid sub-agent config format: {agent_config}")
            return None
    
    def _get_output_type(self, agent_cfg: Dict[str, Any]) -> Optional[Type[BaseModel]]:
        """Get output type from configuration schema."""
        if "output_schema" in agent_cfg:
            return self._create_output_model_from_schema(agent_cfg["output_schema"])
        return None
    
    def _initialize_message_storage(self) -> MessageStorageBase:
        """
        Initialize message database based on configuration.
        
        Returns:
            MessageStorageBase instance (MessageStorageLocal or MessageStorageRedis)
            
        Note:
            Returns appropriate storage backend based on configuration.
            Defaults to MessageStorageLocal if storage type is not recognized.
        """
        agent_cfg = self.config.get("agent", {})
        message_storage = agent_cfg.get("message_storage", "local")

        message_storage_map = {
            "local": MessageStorageLocal,  # Local mode uses in-memory storage
            "redis": MessageStorageRedis,
        }

        return message_storage_map.get(message_storage, MessageStorageLocal)()

    def _initialize_memory_storage(self) -> MemoryStorageBase:
        """
        Initialize memory storage based on configuration.
        
        Returns:
            MemoryStorageBase instance (MemoryStorageLocal or MemoryStorageUpstash)
            
        Note:
            Returns appropriate memory backend based on configuration.
            Defaults to MemoryStorageLocal if storage type is not recognized.
        """
        agent_cfg = self.config.get("agent", {})
        memory_storage = agent_cfg.get("memory_storage", "local")

        memory_storage_map = {
            "local": MemoryStorageLocal,  # Local mode uses ChromaDB
            "upstash": MemoryStorageUpstash,
        }

        return memory_storage_map.get(memory_storage, MemoryStorageLocal)()