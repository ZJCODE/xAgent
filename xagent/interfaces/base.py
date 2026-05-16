import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

# Third-party imports
import yaml
from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field, create_model

# Local imports
from ..core.agent import Agent
from ..core.config import AgentConfig
from ..core.providers import SDK_OPENAI, normalize_provider_name, normalize_sdk, provider_config_sdk
from ..components import MessageStorageBase, MessageStorageLocal
from ..integrations.langfuse import ObservabilityRuntime, create_observability_runtime
from ..tools import create_web_search_tool, run_command
from ..tools.search_tool import (
    SEARCH_PROVIDER_OPENAI,
    is_placeholder_api_key,
    normalize_search_provider,
)


class BaseAgentConfig:
    """Configuration constants for BaseAgentRunner."""

    DEFAULT_MODEL = AgentConfig.DEFAULT_MODEL
    DEFAULT_CONFIG_DIR = AgentConfig.DEFAULT_WORKSPACE
    MEMORY_DIRNAME = AgentConfig.MEMORY_DIRNAME
    MESSAGE_DIRNAME = AgentConfig.MESSAGE_DIRNAME
    MESSAGE_DB_FILENAME = AgentConfig.MESSAGE_DB_FILENAME
    CONFIG_FILENAME = "config.yaml"
    IDENTITY_FILENAME = "identity.md"
    DEFAULT_HOST = "127.0.0.1"
    DEFAULT_PORT = 8010
    RUNTIME_HEARTBEAT_ENABLED = AgentConfig.RUNTIME_HEARTBEAT_ENABLED
    RUNTIME_HEARTBEAT_INTERVAL_SECONDS = AgentConfig.RUNTIME_HEARTBEAT_INTERVAL_SECONDS

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
        self.identity_path = self.config_dir / BaseAgentConfig.IDENTITY_FILENAME
        
        # Load and validate configuration
        self.config = self._load_config(self.config_path)
        self.identity = self._load_identity(self.identity_path)
        
        # Local runtime data lives beside config.yaml.
        self.workspace = self.config_dir
        self.observability = self._initialize_observability(self.config)

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
            raise FileNotFoundError(f"Configuration file not found: {cfg_path}")
        
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
        
        allowed_config_keys = {
            "provider",
            "search",
            "output_schema",
            "channels",
            "runtime",
            "observability",
        }
        unsupported_keys = sorted(set(config) - allowed_config_keys)
        if unsupported_keys:
            joined_keys = ", ".join(unsupported_keys)
            raise ValueError(f"Unsupported config key(s): {joined_keys}")

        channels_cfg = config.get("channels")
        if channels_cfg is not None and not isinstance(channels_cfg, dict):
            raise ValueError("channels must be a dictionary")
        if isinstance(channels_cfg, dict):
            allowed_channel_keys = {"api", "feishu"}
            unsupported_channel_keys = sorted(set(channels_cfg) - allowed_channel_keys)
            if unsupported_channel_keys:
                joined_keys = ", ".join(unsupported_channel_keys)
                raise ValueError(f"Unsupported channels key(s): {joined_keys}")

        runtime_cfg = config.get("runtime")
        if runtime_cfg is not None and not isinstance(runtime_cfg, dict):
            raise ValueError("runtime must be a dictionary")
        if isinstance(runtime_cfg, dict):
            allowed_runtime_keys = {
                "default_channel",
                "heartbeat_enabled",
                "heartbeat_interval_seconds",
            }
            unsupported_runtime_keys = sorted(set(runtime_cfg) - allowed_runtime_keys)
            if unsupported_runtime_keys:
                joined_keys = ", ".join(unsupported_runtime_keys)
                raise ValueError(f"Unsupported runtime key(s): {joined_keys}")
            if "default_channel" in runtime_cfg:
                default_channel = runtime_cfg.get("default_channel")
                if default_channel not in {"api", "feishu", "all"}:
                    raise ValueError("runtime.default_channel must be one of: api, feishu, all")
            if "heartbeat_enabled" in runtime_cfg and not isinstance(runtime_cfg["heartbeat_enabled"], bool):
                raise ValueError("runtime.heartbeat_enabled must be a boolean")
            if "heartbeat_interval_seconds" in runtime_cfg:
                self._validate_positive_number(
                    runtime_cfg["heartbeat_interval_seconds"],
                    "runtime.heartbeat_interval_seconds",
                )

        self._validate_observability_config(config.get("observability"))

        provider_cfg = config.get("provider")
        if not isinstance(provider_cfg, dict) or not provider_cfg:
            raise ValueError("provider is required")

        provider_model = provider_cfg.get("model")
        if not isinstance(provider_model, str) or not provider_model.strip():
            raise ValueError("provider.model is required")
        self._validate_provider_config(provider_cfg)
        if "max_tokens" in provider_cfg:
            self._validate_positive_int(provider_cfg["max_tokens"], "provider.max_tokens")

        self._validate_search_config(config.get("search"), provider_cfg)
        
        return config

    def _validate_observability_config(self, observability_cfg: Optional[Dict[str, Any]]) -> None:
        """Validate optional Langfuse observability configuration."""
        if observability_cfg is None:
            return
        if not isinstance(observability_cfg, dict):
            raise ValueError("observability must be a dictionary")

        allowed_observability_keys = {
            "enabled",
            "provider",
            "public_key",
            "secret_key",
            "base_url",
            "sample_rate",
            "debug",
            "tracing_enabled",
        }
        unsupported_observability_keys = sorted(set(observability_cfg) - allowed_observability_keys)
        if unsupported_observability_keys:
            joined_keys = ", ".join(unsupported_observability_keys)
            raise ValueError(f"Unsupported observability key(s): {joined_keys}")

        if "enabled" in observability_cfg and not isinstance(observability_cfg["enabled"], bool):
            raise ValueError("observability.enabled must be a boolean")
        if not observability_cfg.get("enabled", False):
            return

        provider = observability_cfg.get("provider")
        if not isinstance(provider, str) or provider.strip().lower() != "langfuse":
            raise ValueError("observability.provider must be langfuse")

        for key in ("public_key", "secret_key"):
            value = observability_cfg.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"observability.{key} is required when observability is enabled")

        if "base_url" in observability_cfg:
            base_url = observability_cfg.get("base_url")
            if not isinstance(base_url, str) or not base_url.strip():
                raise ValueError("observability.base_url must be a non-empty string")
        if "sample_rate" in observability_cfg:
            sample_rate = observability_cfg.get("sample_rate")
            if (
                isinstance(sample_rate, bool)
                or not isinstance(sample_rate, (int, float))
                or sample_rate < 0
                or sample_rate > 1
            ):
                raise ValueError("observability.sample_rate must be between 0 and 1")
        for key in ("debug", "tracing_enabled"):
            if key in observability_cfg and not isinstance(observability_cfg[key], bool):
                raise ValueError(f"observability.{key} must be a boolean")

    @staticmethod
    def _validate_positive_int(value: Any, name: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")

    @staticmethod
    def _validate_positive_number(value: Any, name: str) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"{name} must be a positive number")

    @staticmethod
    def _validate_non_negative_number(value: Any, name: str) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"{name} must be a non-negative number")

    @staticmethod
    def _validate_provider_config(provider_cfg: Dict[str, Any]) -> None:
        allowed_provider_keys = {
            "name",
            "sdk",
            "base_url",
            "api_key",
            "model",
            "max_tokens",
        }
        unsupported_provider_keys = sorted(set(provider_cfg) - allowed_provider_keys)
        if unsupported_provider_keys:
            joined_keys = ", ".join(unsupported_provider_keys)
            raise ValueError(f"Unsupported provider key(s): {joined_keys}")

        provider_name = normalize_provider_name(provider_cfg.get("name"))
        if "sdk" in provider_cfg:
            normalize_sdk(provider_cfg.get("sdk"))
        if provider_name == "custom" and "sdk" not in provider_cfg:
            raise ValueError("provider.sdk is required when provider.name is custom")
        if provider_name and provider_name != "custom" and "sdk" in provider_cfg:
            raise ValueError("provider.sdk is only supported when provider.name is custom")

    def _validate_search_config(
        self,
        search_cfg: Optional[Dict[str, Any]],
        provider_cfg: Dict[str, Any],
    ) -> None:
        """Validate optional search configuration."""
        if search_cfg is None:
            return
        if not isinstance(search_cfg, dict):
            raise ValueError("search must be a dictionary")

        search_provider = normalize_search_provider(search_cfg.get("provider"))
        if search_provider == SEARCH_PROVIDER_OPENAI and not self._is_openai_provider(provider_cfg):
            api_key = str(search_cfg.get("api_key") or "").strip()
            if is_placeholder_api_key(api_key):
                raise ValueError(
                    "search.provider 'openai' requires search.api_key when provider is not OpenAI"
                )

    @staticmethod
    def _is_openai_provider(provider_cfg: Dict[str, Any]) -> bool:
        sdk = provider_config_sdk(provider_cfg)
        if sdk != SDK_OPENAI:
            return False

        provider_name = str(provider_cfg.get("name") or "").strip().lower()
        if provider_name:
            return provider_name == "openai"

        base_url = str(provider_cfg.get("base_url") or "").strip().rstrip("/")
        if not base_url:
            return True
        return base_url == "https://api.openai.com/v1"

    def _load_identity(self, identity_path: Path) -> str:
        """Load config-driven agent identity instructions from identity.md."""
        if not identity_path.is_file():
            raise FileNotFoundError(f"Identity file not found: {identity_path}")
        identity = identity_path.read_text(encoding="utf-8").strip()
        if not identity:
            raise ValueError(f"Identity file is empty: {identity_path}")
        return identity
    
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
        agent_cfg = self.config
        
        client = self._initialize_client(agent_cfg)
        tools = self._load_agent_tools(agent_cfg, client=client)
        output_type = self._get_output_type(agent_cfg)

        return Agent(
            system_prompt=self.identity,
            model=self._get_agent_model(agent_cfg),
            model_backend=self._get_provider_backend(agent_cfg),
            model_max_tokens=self._get_provider_max_tokens(agent_cfg),
            client=client,
            tools=tools,
            output_type=output_type,
            message_storage=self.message_storage,
            workspace=str(self.workspace),
            observability=self.observability,
        )

    def _initialize_observability(self, agent_cfg: Dict[str, Any]) -> ObservabilityRuntime:
        """Build the optional observability runtime."""
        return create_observability_runtime(agent_cfg.get("observability"))

    def _get_agent_model(self, agent_cfg: Dict[str, Any]) -> Optional[str]:
        """Read the model from provider.model."""
        provider_cfg = agent_cfg.get("provider") or {}
        if isinstance(provider_cfg, dict) and provider_cfg.get("model"):
            return provider_cfg.get("model")
        return None

    def _get_provider_backend(self, agent_cfg: Dict[str, Any]) -> str:
        provider_cfg = agent_cfg.get("provider") or {}
        if isinstance(provider_cfg, dict):
            return provider_config_sdk(provider_cfg)
        return SDK_OPENAI

    def _get_provider_max_tokens(self, agent_cfg: Dict[str, Any]) -> int:
        provider_cfg = agent_cfg.get("provider") or {}
        if isinstance(provider_cfg, dict) and provider_cfg.get("max_tokens"):
            return int(provider_cfg["max_tokens"])
        return AgentConfig.DEFAULT_MAX_TOKENS

    def _initialize_client(self, agent_cfg: Dict[str, Any]) -> Optional[Any]:
        """Build an async model client from optional provider config."""
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

        if self._get_provider_backend(agent_cfg) == "anthropic":
            return AsyncAnthropic(**client_kwargs)

        return self.observability.create_client(client_kwargs)

    def _initialize_search_client(
        self,
        agent_cfg: Dict[str, Any],
        *,
        model_client: Optional[Any],
    ) -> Optional[Any]:
        """Build the client used by OpenAI built-in search."""
        search_cfg = agent_cfg.get("search") or {}
        if not isinstance(search_cfg, dict):
            return model_client

        search_provider = normalize_search_provider(search_cfg.get("provider"))
        if search_provider != SEARCH_PROVIDER_OPENAI:
            return model_client

        provider_cfg = agent_cfg.get("provider") or {}
        is_openai_provider = isinstance(provider_cfg, dict) and self._is_openai_provider(provider_cfg)
        search_api_key = str(search_cfg.get("api_key") or "").strip()
        if search_api_key and not is_placeholder_api_key(search_api_key):
            return self.observability.create_client({"api_key": search_api_key})

        if is_openai_provider:
            return model_client
        return None

    def _get_search_model(self, agent_cfg: Dict[str, Any]) -> Optional[str]:
        search_cfg = agent_cfg.get("search") or {}
        if isinstance(search_cfg, dict) and search_cfg.get("model"):
            return search_cfg.get("model")

        provider_cfg = agent_cfg.get("provider") or {}
        configured_search_provider = search_cfg.get("provider") if isinstance(search_cfg, dict) else None
        search_provider = normalize_search_provider(configured_search_provider)
        if search_provider == SEARCH_PROVIDER_OPENAI:
            if not isinstance(provider_cfg, dict) or not self._is_openai_provider(provider_cfg):
                return AgentConfig.DEFAULT_MODEL
        return self._get_agent_model(agent_cfg)
    
    def _load_agent_tools(
        self,
        agent_cfg: Dict[str, Any],
        *,
        client: Optional[Any] = None,
    ) -> List[Any]:
        """Load default built-in tools."""
        if "capabilities" in agent_cfg or "tools" in agent_cfg:
            self.logger.warning("Configured tools are ignored; run_command is built in by default.")

        tools = [run_command]
        search_client = self._initialize_search_client(agent_cfg, model_client=client)
        search_tool = create_web_search_tool(
            agent_cfg.get("search"),
            client=search_client,
            model=self._get_search_model(agent_cfg),
        )
        if search_tool is not None:
            tools.append(search_tool)
        return tools
    
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
        return self._create_message_storage()

    def _get_message_storage_path(self) -> Path:
        return self.workspace / BaseAgentConfig.MESSAGE_DIRNAME / BaseAgentConfig.MESSAGE_DB_FILENAME

    def _create_message_storage(self) -> MessageStorageBase:
        return MessageStorageLocal(path=str(self._get_message_storage_path()))
