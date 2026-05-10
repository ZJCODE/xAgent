import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

# Third-party imports
import yaml
from openai import AsyncOpenAI
from pydantic import BaseModel, Field, create_model

# Local imports
from ..core.agent import Agent
from ..core.config import AgentConfig
from ..components import MessageStorageBase, MessageStorageLocal
from ..tools import run_command


class BaseAgentConfig:
    """Configuration constants for BaseAgentRunner."""
    
    DEFAULT_AGENT_NAME = "Agent"
    DEFAULT_SYSTEM_PROMPT = """
You are a warm, friendly, and engaging AI assistant who speaks in a natural, casual, and concise way, like a close friend chatting in everyday life. Your vibe is gentle, relaxed, and comforting—approachable, lively, and easy to talk to, but never childish, exaggerated, or forced. You are good at keeping conversations smooth and enjoyable, answering questions clearly, and giving practical advice without sounding formal, robotic, or preachy.

You can chat playfully, offer emotional support, share interesting things, and help solve problems depending on the user’s mood and needs. Your replies should usually be short, easygoing, and human-like, unless more detail is truly needed.

You are also emotionally responsive and easy to talk to: you do not casually reject, dismiss, or shut down the user’s ideas, and you try to respond in a supportive, receptive, and non-disappointing way first, keeping the conversation pleasant and encouraging. Even when you need to disagree, correct something, or point out a problem, you do so gently and tactfully without killing the mood.

If you are unsure about something, be honest instead of making things up. If you encounter a new meme, trend, or unfamiliar online expression, you may proactively look it up and explain it in a simple, natural, and easy-to-understand way.

Always make the conversation feel light, genuine, warm, and like talking with a real friend.
"""
    DEFAULT_MODEL = AgentConfig.DEFAULT_MODEL
    DEFAULT_CONFIG_DIR = AgentConfig.DEFAULT_WORKSPACE
    CONFIG_FILENAME = "config.yaml"
    DEFAULT_HOST = "0.0.0.0"
    DEFAULT_PORT = 8010

class BaseAgentRunner:
    """
    Base class for agent runners with common configuration and initialization logic.
    
    This class provides a standardized way to:
    - Load and validate agent configurations from YAML files
    - Initialize agents with tools
    - Manage message databases
    - Create dynamic Pydantic models from schema definitions
    
    Attributes:
        config: Loaded configuration dictionary
        agent: Initialized Agent instance
        message_storage: Optional message storage instance
        config_dir: Directory containing config.yaml and local runtime data
    """
    
    def __init__(
        self, 
        config_dir: Optional[str] = None,
    ):
        """
        Initialize BaseAgentRunner.
        
        Args:
            config_dir: Directory containing config.yaml. If None, uses ~/.xagent
            
        Raises:
            yaml.YAMLError: If configuration file is invalid
        """
        self.logger = logging.getLogger(self.__class__.__name__)
        
        self.config_dir = self._resolve_config_dir(config_dir)
        self.config_path = self.config_dir / BaseAgentConfig.CONFIG_FILENAME
        
        # Load and validate configuration
        self.config = self._load_config(self.config_path)
        
        # Local runtime data lives beside config.yaml.
        self.workspace = self.config_dir

        # Initialize components in dependency order
        self.message_storage = self._initialize_message_storage()
        self.agent = self._initialize_agent()
        
    def _resolve_config_dir(self, config_dir: Optional[str]) -> Path:
        """Resolve the xAgent runtime directory."""
        raw_dir = config_dir or BaseAgentConfig.DEFAULT_CONFIG_DIR
        return Path(raw_dir).expanduser().resolve()

    def _load_config(self, cfg_path: Path) -> Dict[str, Any]:
        """
        Load YAML configuration file with error handling.
        
        Args:
            cfg_path: Path to config.yaml
            
        Returns:
            Configuration dictionary
            
        Raises:
            yaml.YAMLError: If YAML file is malformed
        """
        if not cfg_path.is_file():
            return self._get_default_config()
        
        try:
            with cfg_path.open("r", encoding="utf-8") as f:
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
        if "provider" not in config["agent"]:
            config["agent"]["provider"] = {}
        
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
                "provider": {
                    "model": BaseAgentConfig.DEFAULT_MODEL,
                },
            }
        }
    
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
            self.logger.warning("Failed to create output model from schema: %s", e)
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
        
        tools = self._load_agent_tools(agent_cfg)
        output_type = self._get_output_type(agent_cfg)
        client = self._initialize_client(agent_cfg)

        return Agent(
            name=agent_cfg.get("name"),
            system_prompt=agent_cfg.get("system_prompt"),
            model=self._get_agent_model(agent_cfg),
            client=client,
            tools=tools,
            output_type=output_type,
            message_storage=self.message_storage,
            workspace=str(self.workspace),
        )

    def _get_agent_model(self, agent_cfg: Dict[str, Any]) -> Optional[str]:
        """Read the model from provider.model, with legacy agent.model fallback."""
        provider_cfg = agent_cfg.get("provider") or {}
        if isinstance(provider_cfg, dict) and provider_cfg.get("model"):
            return provider_cfg.get("model")
        return agent_cfg.get("model")

    def _initialize_client(self, agent_cfg: Dict[str, Any]) -> Optional[AsyncOpenAI]:
        """Build an OpenAI-compatible async client from optional provider config."""
        provider_cfg = agent_cfg.get("provider") or {}
        if not provider_cfg or not isinstance(provider_cfg, dict):
            return None

        client_kwargs: Dict[str, Any] = {}
        base_url = provider_cfg.get("base_url")
        if base_url:
            client_kwargs["base_url"] = base_url

        api_key = provider_cfg.get("api_key")
        if api_key:
            client_kwargs["api_key"] = api_key

        if not client_kwargs:
            return None

        return AsyncOpenAI(**client_kwargs)
    
    def _load_agent_tools(self, agent_cfg: Dict[str, Any]) -> List[Any]:
        """Load default built-in tools."""
        if "capabilities" in agent_cfg or "tools" in agent_cfg:
            self.logger.warning("Configured tools are ignored; run_command is built in by default.")
        return [run_command]
    
    def _get_output_type(self, agent_cfg: Dict[str, Any]) -> Optional[Type[BaseModel]]:
        """Get output type from configuration schema."""
        if "output_schema" in agent_cfg:
            return self._create_output_model_from_schema(agent_cfg["output_schema"])
        return None
    
    def _initialize_message_storage(self) -> MessageStorageBase:
        """
        Initialize the default message storage backend.

        Subclasses can override `_create_message_storage` to plug in a different
        implementation while keeping the runner lifecycle unchanged.
        """
        agent_name = self._get_agent_name()
        agent_slug = self._normalize_agent_identifier(agent_name)
        return self._create_message_storage(agent_name=agent_name, agent_slug=agent_slug)

    def _get_agent_name(self) -> str:
        return self.config.get("agent", {}).get("name", AgentConfig.DEFAULT_NAME)

    def _get_message_storage_path(self, agent_slug: str) -> Path:
        return self.workspace / f"{agent_slug}_messages.sqlite3"

    def _create_message_storage(
        self,
        *,
        agent_name: str,
        agent_slug: str,
    ) -> MessageStorageBase:
        del agent_name
        return MessageStorageLocal(path=str(self._get_message_storage_path(agent_slug)))

    @staticmethod
    def _normalize_agent_identifier(name: str) -> str:
        return (name or AgentConfig.DEFAULT_NAME).lower().replace(" ", "_").replace("-", "_")
