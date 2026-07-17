import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

# Third-party imports
import yaml
from anthropic import AsyncAnthropic
# Local imports
from ..core.agent import Agent
from ..core.config import AgentConfig
from ..core.providers import (
    ReasoningConfig,
    model_api_uses_anthropic_client,
    normalize_reasoning_config,
    normalize_provider_name,
    normalize_model_api,
    provider_is_official_openai,
    provider_base_url,
    provider_model_api,
    provider_supports_vision,
    resolved_provider_name,
    PROVIDER_MINIMAX,
    PROVIDER_QWEN,
)
from ..components import MessageStorage
from ..components.skills import SkillsStorageBase, SkillsStorageLocal
from ..integrations.langfuse import ObservabilityRuntime, create_observability_runtime
from ..tools import (
    create_attach_artifact_tool,
    create_image_generation_tool,
    create_read_skill_tool,
    create_schedule_task_tool,
    create_web_fetch_tool,
    create_web_search_tool,
    create_workspace_run_command_tool,
)
from ..tools.image_generation_tool import (
    IMAGE_GENERATION_PROVIDER_NONE,
    IMAGE_GENERATION_PROVIDER_OPENAI,
    normalize_image_generation_provider,
    IMAGE_GENERATION_PROVIDER_MINIMAX,
    IMAGE_GENERATION_PROVIDER_QWEN,
)
from ..tools.search_tool import (
    DEFAULT_QWEN_SEARCH_MODEL,
    SEARCH_PROVIDER_MINIMAX,
    SEARCH_PROVIDER_OPENAI,
    SEARCH_PROVIDER_QWEN,
    is_placeholder_api_key,
    normalize_search_provider,
)


class BaseAgentConfig:
    """Configuration constants for BaseAgentRunner."""

    DEFAULT_MODEL = AgentConfig.DEFAULT_MODEL
    DEFAULT_CONFIG_DIR = AgentConfig.DEFAULT_WORKSPACE
    MEMORY_DIRNAME = AgentConfig.MEMORY_DIRNAME
    MESSAGE_DIRNAME = AgentConfig.MESSAGE_DIRNAME
    WORKSPACE_DIRNAME = AgentConfig.WORKSPACE_DIRNAME
    SKILLS_DIRNAME = AgentConfig.SKILLS_DIRNAME
    TASKS_DIRNAME = AgentConfig.TASKS_DIRNAME
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
        self.workspace_dir = self.workspace / BaseAgentConfig.WORKSPACE_DIRNAME
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir = self.workspace / BaseAgentConfig.SKILLS_DIRNAME
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.tasks_dir = self.workspace / BaseAgentConfig.TASKS_DIRNAME
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.observability = self._initialize_observability(self.config)

        # Initialize components in dependency order
        self.message_storage = self._initialize_message_storage()
        self.skills_storage = self._initialize_skills_storage()
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
            "agent",
            "provider",
            "search",
            "image_generation",
            "channels",
            "runtime",
            "observability",
            "web",
        }
        unsupported_keys = sorted(set(config) - allowed_config_keys)
        if unsupported_keys:
            joined_keys = ", ".join(unsupported_keys)
            raise ValueError(f"Unsupported config key(s): {joined_keys}")

        channels_cfg = config.get("channels")
        if channels_cfg is not None and not isinstance(channels_cfg, dict):
            raise ValueError("channels must be a dictionary")
        if isinstance(channels_cfg, dict):
            allowed_channel_keys = {"api", "feishu", "weixin", "voice"}
            unsupported_channel_keys = sorted(set(channels_cfg) - allowed_channel_keys)
            if unsupported_channel_keys:
                joined_keys = ", ".join(unsupported_channel_keys)
                raise ValueError(f"Unsupported channels key(s): {joined_keys}")
            voice_cfg = channels_cfg.get("voice")
            if voice_cfg is not None:
                from .voice.config import VoiceChannelConfig

                VoiceChannelConfig.from_dict(voice_cfg)

        web_cfg = config.get("web")
        if web_cfg is not None:
            if not isinstance(web_cfg, dict):
                raise ValueError("web must be a dictionary")
            allowed_web_keys = {"enabled", "api_url"}
            unsupported_web_keys = sorted(set(web_cfg) - allowed_web_keys)
            if unsupported_web_keys:
                joined_keys = ", ".join(unsupported_web_keys)
                raise ValueError(f"Unsupported web key(s): {joined_keys}")
            if "enabled" in web_cfg and not isinstance(web_cfg["enabled"], bool):
                raise ValueError("web.enabled must be a boolean")
            if "api_url" in web_cfg and not isinstance(web_cfg["api_url"], str):
                raise ValueError("web.api_url must be a string")

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
                if default_channel not in {"api", "feishu", "weixin", "voice"}:
                    raise ValueError("runtime.default_channel must be one of: api, feishu, weixin, voice")
            if "heartbeat_enabled" in runtime_cfg and not isinstance(runtime_cfg["heartbeat_enabled"], bool):
                raise ValueError("runtime.heartbeat_enabled must be a boolean")
            if "heartbeat_interval_seconds" in runtime_cfg:
                self._validate_positive_number(
                    runtime_cfg["heartbeat_interval_seconds"],
                    "runtime.heartbeat_interval_seconds",
                )

        agent_cfg = config.get("agent")
        if agent_cfg is not None:
            if not isinstance(agent_cfg, dict):
                raise ValueError("agent must be a dictionary")
            allowed_agent_keys = {
                "max_history",
                "max_iter",
                "max_concurrent_tools",
                "subconscious_activity",
                "memory_recent_days",
            }
            unsupported_agent_keys = sorted(set(agent_cfg) - allowed_agent_keys)
            if unsupported_agent_keys:
                joined_keys = ", ".join(unsupported_agent_keys)
                raise ValueError(f"Unsupported agent key(s): {joined_keys}")
            for key in ("max_history", "max_iter", "max_concurrent_tools"):
                if key in agent_cfg:
                    self._validate_positive_int(agent_cfg[key], f"agent.{key}")
            if "subconscious_activity" in agent_cfg:
                val = agent_cfg["subconscious_activity"]
                if not isinstance(val, (int, float)) or not (0 <= val <= 1):
                    raise ValueError(
                        f"agent.subconscious_activity must be a number between 0 and 1, got {val!r}"
                    )
            if "memory_recent_days" in agent_cfg:
                self._validate_non_negative_int(agent_cfg["memory_recent_days"], "agent.memory_recent_days")
        self._validate_observability_config(config.get("observability"))

        provider_cfg = config.get("provider")
        if not isinstance(provider_cfg, dict) or not provider_cfg:
            raise ValueError("provider is required")

        provider_model = provider_cfg.get("model")
        if not isinstance(provider_model, str) or not provider_model.strip():
            raise ValueError("provider.model is required")
        if "max_tokens" in provider_cfg:
            self._validate_positive_int(provider_cfg["max_tokens"], "provider.max_tokens")
        self._validate_provider_config(provider_cfg)

        self._validate_search_config(config.get("search"), provider_cfg)
        self._validate_image_generation_config(config.get("image_generation"), provider_cfg)
        
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
    def _validate_non_negative_int(value: Any, name: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")

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
            "model_api",
            "base_url",
            "api_key",
            "model",
            "max_tokens",
            "reasoning",
            "supports_vision",
        }
        unsupported_provider_keys = sorted(set(provider_cfg) - allowed_provider_keys)
        if unsupported_provider_keys:
            joined_keys = ", ".join(unsupported_provider_keys)
            raise ValueError(f"Unsupported provider key(s): {joined_keys}")

        provider_name = normalize_provider_name(provider_cfg.get("name"))
        if "model_api" in provider_cfg:
            normalize_model_api(provider_cfg.get("model_api"))
        if provider_name == "custom" and "model_api" not in provider_cfg:
            raise ValueError("provider.model_api is required when provider.name is custom")
        if provider_name and provider_name != "custom" and "model_api" in provider_cfg:
            raise ValueError("provider.model_api is only supported when provider.name is custom")
        if "supports_vision" in provider_cfg:
            if not isinstance(provider_cfg["supports_vision"], bool):
                raise ValueError("provider.supports_vision must be a boolean")
        reasoning = normalize_reasoning_config(provider_cfg)
        if reasoning is not None:
            provider_cfg["reasoning"] = reasoning.to_dict()

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
        if search_provider == SEARCH_PROVIDER_QWEN and not self._is_qwen_provider(provider_cfg):
            api_key = str(search_cfg.get("api_key") or "").strip()
            if is_placeholder_api_key(api_key):
                raise ValueError(
                    "search.provider 'qwen' requires search.api_key when provider is not Qwen"
                )
        if search_provider == SEARCH_PROVIDER_MINIMAX and not self._is_minimax_provider(provider_cfg):
            api_key = str(search_cfg.get("api_key") or "").strip()
            if is_placeholder_api_key(api_key):
                raise ValueError(
                    "search.provider 'minimax' requires search.api_key when provider is not MiniMax"
                )

    def _validate_image_generation_config(
        self,
        image_generation_cfg: Optional[Dict[str, Any]],
        provider_cfg: Dict[str, Any],
    ) -> None:
        """Validate optional image generation configuration."""
        if image_generation_cfg is None:
            return
        if not isinstance(image_generation_cfg, dict):
            raise ValueError("image_generation must be a dictionary")

        allowed_image_generation_keys = {
            "provider",
            "api_key",
            "base_url",
            "endpoint",
            "model",
            "size",
            "quality",
            "output_format",
            "background",
            "output_compression",
            "moderation",
            "negative_prompt",
            "prompt_extend",
            "watermark",
            "aspect_ratio",
            "width",
            "height",
            "n",
            "seed",
            "prompt_optimizer",
            "aigc_watermark",
            "reference_image_url",
            "reference_image_urls",
            "subject_reference",
            "style",
        }
        unsupported_keys = sorted(set(image_generation_cfg) - allowed_image_generation_keys)
        if unsupported_keys:
            joined_keys = ", ".join(unsupported_keys)
            raise ValueError(f"Unsupported image_generation key(s): {joined_keys}")

        image_generation_provider = normalize_image_generation_provider(image_generation_cfg.get("provider"))
        if image_generation_provider == IMAGE_GENERATION_PROVIDER_OPENAI and not self._is_openai_provider(provider_cfg):
            api_key = str(image_generation_cfg.get("api_key") or "").strip()
            if is_placeholder_api_key(api_key):
                raise ValueError(
                    "image_generation.provider 'openai' requires image_generation.api_key when provider is not OpenAI"
                )
        if image_generation_provider == IMAGE_GENERATION_PROVIDER_MINIMAX and not self._is_minimax_provider(provider_cfg):
            api_key = str(image_generation_cfg.get("api_key") or "").strip()
            if is_placeholder_api_key(api_key):
                raise ValueError(
                    "image_generation.provider 'minimax' requires image_generation.api_key when provider is not MiniMax"
                )
        if image_generation_provider == IMAGE_GENERATION_PROVIDER_QWEN and not self._is_qwen_provider(provider_cfg):
            api_key = str(image_generation_cfg.get("api_key") or "").strip()
            if is_placeholder_api_key(api_key):
                raise ValueError(
                    "image_generation.provider 'qwen' requires image_generation.api_key when provider is not Qwen"
                )
        if image_generation_provider not in {
            IMAGE_GENERATION_PROVIDER_NONE,
            IMAGE_GENERATION_PROVIDER_OPENAI,
            IMAGE_GENERATION_PROVIDER_MINIMAX,
            IMAGE_GENERATION_PROVIDER_QWEN,
        }:
            raise ValueError("image_generation.provider must be one of: none, openai, minimax, qwen")
        if "size" in image_generation_cfg:
            value = str(image_generation_cfg["size"] or "").strip().lower()
            if image_generation_provider == IMAGE_GENERATION_PROVIDER_OPENAI:
                allowed = {"auto", "1024x1024", "1024x1536", "1536x1024"}
                if value not in allowed:
                    raise ValueError("image_generation.size must be one of: auto, 1024x1024, 1024x1536, 1536x1024")
            if image_generation_provider == IMAGE_GENERATION_PROVIDER_QWEN:
                normalized = value.replace("x", "*")
                if normalized != "auto" and "*" not in normalized:
                    raise ValueError("image_generation.size must be auto or WIDTH*HEIGHT for Qwen")
        if "quality" in image_generation_cfg:
            value = str(image_generation_cfg["quality"] or "").strip().lower()
            if image_generation_provider == IMAGE_GENERATION_PROVIDER_OPENAI and value not in {"auto", "low", "medium", "high"}:
                raise ValueError("image_generation.quality must be one of: auto, low, medium, high")
        if "output_format" in image_generation_cfg:
            value = str(image_generation_cfg["output_format"] or "").strip().lower()
            if value == "jpg":
                value = "jpeg"
            if value not in {"png", "jpeg", "webp"}:
                raise ValueError("image_generation.output_format must be one of: png, jpeg, webp")
        if "background" in image_generation_cfg:
            value = str(image_generation_cfg["background"] or "").strip().lower()
            if image_generation_provider == IMAGE_GENERATION_PROVIDER_OPENAI and value not in {"auto", "opaque", "transparent"}:
                raise ValueError("image_generation.background must be one of: auto, opaque, transparent")
        if "moderation" in image_generation_cfg:
            value = str(image_generation_cfg["moderation"] or "").strip().lower()
            if image_generation_provider == IMAGE_GENERATION_PROVIDER_OPENAI and value not in {"auto", "low"}:
                raise ValueError("image_generation.moderation must be one of: auto, low")
        if "aspect_ratio" in image_generation_cfg:
            value = str(image_generation_cfg["aspect_ratio"] or "").strip()
            allowed_aspect_ratios = {"1:1", "16:9", "4:3", "3:2", "2:3", "3:4", "9:16", "21:9"}
            if image_generation_provider == IMAGE_GENERATION_PROVIDER_MINIMAX and value not in allowed_aspect_ratios:
                raise ValueError("image_generation.aspect_ratio must be a supported MiniMax aspect ratio")
        if "output_compression" in image_generation_cfg:
            value = image_generation_cfg["output_compression"]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 100:
                raise ValueError("image_generation.output_compression must be an integer from 0 to 100")
        if "n" in image_generation_cfg:
            value = image_generation_cfg["n"]
            if image_generation_provider == IMAGE_GENERATION_PROVIDER_MINIMAX:
                max_images = 9
            elif image_generation_provider == IMAGE_GENERATION_PROVIDER_QWEN:
                max_images = 6
            else:
                max_images = 10
            if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > max_images:
                raise ValueError(f"image_generation.n must be an integer from 1 to {max_images}")
        for key in ("width", "height"):
            if key in image_generation_cfg:
                value = image_generation_cfg[key]
                if isinstance(value, bool) or not isinstance(value, int) or value < 512 or value > 2048 or value % 8 != 0:
                    raise ValueError(f"image_generation.{key} must be an integer from 512 to 2048 and a multiple of 8")
        for key in ("prompt_optimizer", "aigc_watermark"):
            if key in image_generation_cfg and not isinstance(image_generation_cfg[key], bool):
                raise ValueError(f"image_generation.{key} must be a boolean")
        for key in ("prompt_extend", "watermark"):
            if key in image_generation_cfg and not isinstance(image_generation_cfg[key], bool):
                raise ValueError(f"image_generation.{key} must be a boolean")
        if "reference_image_urls" in image_generation_cfg and not isinstance(image_generation_cfg["reference_image_urls"], list):
            raise ValueError("image_generation.reference_image_urls must be a list")
        if "subject_reference" in image_generation_cfg and not isinstance(image_generation_cfg["subject_reference"], list):
            raise ValueError("image_generation.subject_reference must be a list")
        if "style" in image_generation_cfg and not isinstance(image_generation_cfg["style"], dict):
            raise ValueError("image_generation.style must be a dictionary")

    @staticmethod
    def _is_openai_provider(provider_cfg: Dict[str, Any]) -> bool:
        return provider_is_official_openai(provider_cfg)

    @staticmethod
    def _is_minimax_provider(provider_cfg: Dict[str, Any]) -> bool:
        return normalize_provider_name(provider_cfg.get("name")) == PROVIDER_MINIMAX

    @staticmethod
    def _is_qwen_provider(provider_cfg: Dict[str, Any]) -> bool:
        return normalize_provider_name(provider_cfg.get("name")) == PROVIDER_QWEN

    def _load_identity(self, identity_path: Path) -> str:
        """Load config-driven agent identity instructions from identity.md."""
        if not identity_path.is_file():
            raise FileNotFoundError(f"Identity file not found: {identity_path}")
        identity = identity_path.read_text(encoding="utf-8").strip()
        if not identity:
            raise ValueError(f"Identity file is empty: {identity_path}")
        return identity
    
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

        agent_section = agent_cfg.get("agent") or {}
        return Agent(
            system_prompt=self.identity,
            model=self._get_agent_model(agent_cfg),
            provider_name=self._get_provider_name(agent_cfg),
            model_api=self._get_provider_model_api(agent_cfg),
            model_max_tokens=self._get_provider_max_tokens(agent_cfg),
            reasoning=self._get_provider_reasoning(agent_cfg),
            client=client,
            tools=tools,
            message_storage=self.message_storage,
            workspace=str(self.workspace),
            skills_storage=self.skills_storage,
            observability=self.observability,
            supports_vision=self._provider_supports_vision(agent_cfg),
            max_history=agent_section.get("max_history", AgentConfig.DEFAULT_MAX_HISTORY),
            max_iter=agent_section.get("max_iter", AgentConfig.DEFAULT_MAX_ITER),
            max_concurrent_tools=agent_section.get("max_concurrent_tools", AgentConfig.DEFAULT_MAX_CONCURRENT_TOOLS),
            subconscious_activity=agent_section.get("subconscious_activity", AgentConfig.SUBCONSCIOUS_ACTIVITY),
            memory_recent_days=agent_section.get("memory_recent_days", AgentConfig.MEMORY_RECENT_DAYS),
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

    def _get_provider_model_api(self, agent_cfg: Dict[str, Any]) -> str:
        provider_cfg = agent_cfg.get("provider") or {}
        if isinstance(provider_cfg, dict):
            return provider_model_api(provider_cfg)
        return provider_model_api({})

    def _get_provider_name(self, agent_cfg: Dict[str, Any]) -> str:
        provider_cfg = agent_cfg.get("provider") or {}
        if isinstance(provider_cfg, dict):
            return resolved_provider_name(provider_cfg)
        return ""

    def _get_provider_max_tokens(self, agent_cfg: Dict[str, Any]) -> Optional[int]:
        provider_cfg = agent_cfg.get("provider") or {}
        if isinstance(provider_cfg, dict) and provider_cfg.get("max_tokens"):
            return int(provider_cfg["max_tokens"])
        return None

    def _get_provider_reasoning(self, agent_cfg: Dict[str, Any]) -> Optional[ReasoningConfig]:
        provider_cfg = agent_cfg.get("provider") or {}
        if isinstance(provider_cfg, dict):
            return normalize_reasoning_config(provider_cfg)
        return None

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

        if model_api_uses_anthropic_client(self._get_provider_model_api(agent_cfg)):
            return AsyncAnthropic(**client_kwargs)

        return self.observability.create_client(client_kwargs)

    def _initialize_search_client(
        self,
        agent_cfg: Dict[str, Any],
        *,
        model_client: Optional[Any],
    ) -> Optional[Any]:
        """Build the client used by provider-native search."""
        search_cfg = agent_cfg.get("search") or {}
        if not isinstance(search_cfg, dict):
            return model_client

        search_provider = normalize_search_provider(search_cfg.get("provider"))
        provider_cfg = agent_cfg.get("provider") or {}
        if search_provider == SEARCH_PROVIDER_OPENAI:
            return self._initialize_openai_feature_client(search_cfg, provider_cfg, model_client=model_client)
        if search_provider == SEARCH_PROVIDER_QWEN:
            return self._initialize_qwen_search_client(search_cfg, provider_cfg, model_client=model_client)
        return model_client

    def _initialize_qwen_search_client(
        self,
        search_cfg: Dict[str, Any],
        provider_cfg: Any,
        *,
        model_client: Optional[Any],
    ) -> Optional[Any]:
        api_key = str(search_cfg.get("api_key") or "").strip()
        if api_key and not is_placeholder_api_key(api_key):
            base_url = str(search_cfg.get("base_url") or "").strip()
            if not base_url and isinstance(provider_cfg, dict) and self._is_qwen_provider(provider_cfg):
                base_url = str(provider_cfg.get("base_url") or "").strip()
            if not base_url:
                base_url = provider_base_url(PROVIDER_QWEN)

            client_kwargs: Dict[str, Any] = {
                "api_key": api_key,
                "base_url": base_url,
            }
            return self.observability.create_client(client_kwargs)

        if isinstance(provider_cfg, dict) and self._is_qwen_provider(provider_cfg):
            return model_client

        return None

    def _initialize_image_generation_client(
        self,
        agent_cfg: Dict[str, Any],
        *,
        model_client: Optional[Any],
    ) -> Optional[Any]:
        """Build the client used by OpenAI image generation."""
        image_generation_cfg = agent_cfg.get("image_generation") or {}
        if not isinstance(image_generation_cfg, dict):
            return model_client

        image_generation_provider = normalize_image_generation_provider(image_generation_cfg.get("provider"))
        if image_generation_provider != IMAGE_GENERATION_PROVIDER_OPENAI:
            return model_client

        provider_cfg = agent_cfg.get("provider") or {}
        return self._initialize_openai_feature_client(
            image_generation_cfg,
            provider_cfg,
            model_client=model_client,
        )

    def _initialize_openai_feature_client(
        self,
        feature_cfg: Dict[str, Any],
        provider_cfg: Any,
        *,
        model_client: Optional[Any],
    ) -> Optional[Any]:
        is_openai_provider = isinstance(provider_cfg, dict) and self._is_openai_provider(provider_cfg)
        api_key = str(feature_cfg.get("api_key") or "").strip()
        if api_key and not is_placeholder_api_key(api_key):
            return self.observability.create_client({"api_key": api_key})

        if is_openai_provider:
            return model_client
        return None

    def _provider_supports_vision(self, agent_cfg: Dict[str, Any]) -> bool:
        provider_cfg = agent_cfg.get("provider") or {}
        if isinstance(provider_cfg, dict):
            return provider_supports_vision(provider_cfg)
        return provider_supports_vision({})

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
        if search_provider == SEARCH_PROVIDER_QWEN:
            if not isinstance(provider_cfg, dict) or not self._is_qwen_provider(provider_cfg):
                return DEFAULT_QWEN_SEARCH_MODEL
        return self._get_agent_model(agent_cfg)
    
    def _load_agent_tools(
        self,
        agent_cfg: Dict[str, Any],
        *,
        client: Optional[Any] = None,
    ) -> List[Any]:
        """Load default built-in tools."""
        if "capabilities" in agent_cfg or "tools" in agent_cfg:
            self.logger.warning("Configured tools are ignored; default built-in tools are loaded automatically.")

        tools = [
            create_workspace_run_command_tool(
                default_working_directory=str(self.workspace_dir),
            ),
            create_schedule_task_tool(
                tasks_dir=str(self.tasks_dir),
            ),
            create_attach_artifact_tool(
                workspace_dir=str(self.workspace_dir),
            ),
            create_web_fetch_tool(),
        ]
        search_client = self._initialize_search_client(agent_cfg, model_client=client)
        search_tool = create_web_search_tool(
            self._search_config_for_tools(agent_cfg),
            client=search_client,
            model=self._get_search_model(agent_cfg),
        )
        if search_tool is not None:
            tools.append(search_tool)
        image_generation_client = self._initialize_image_generation_client(agent_cfg, model_client=client)
        image_generation_config = self._image_generation_config_for_tools(agent_cfg)
        image_generation_tool = create_image_generation_tool(
            image_generation_config,
            client=image_generation_client,
            workspace_dir=str(self.workspace_dir),
        )
        if image_generation_tool is not None:
            tools.append(image_generation_tool)
        if getattr(self, "skills_storage", None) is not None:
            tools.append(create_read_skill_tool(self.skills_storage))
        return tools

    def _search_config_for_tools(self, agent_cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        search_cfg = agent_cfg.get("search")
        if not isinstance(search_cfg, dict):
            return search_cfg

        search_provider = normalize_search_provider(search_cfg.get("provider"))
        if search_provider not in {SEARCH_PROVIDER_QWEN, SEARCH_PROVIDER_MINIMAX}:
            return search_cfg

        provider_cfg = agent_cfg.get("provider") or {}
        provider_matches_search = (
            isinstance(provider_cfg, dict)
            and (
                (search_provider == SEARCH_PROVIDER_QWEN and self._is_qwen_provider(provider_cfg))
                or (search_provider == SEARCH_PROVIDER_MINIMAX and self._is_minimax_provider(provider_cfg))
            )
        )
        if not provider_matches_search:
            return search_cfg

        merged_config = dict(search_cfg)
        configured_key = str(search_cfg.get("api_key") or "").strip()
        if not configured_key or is_placeholder_api_key(configured_key):
            provider_key = str(provider_cfg.get("api_key") or "").strip()
            if not is_placeholder_api_key(provider_key):
                merged_config["api_key"] = provider_key

        provider_base_url_value = str(provider_cfg.get("base_url") or "").strip()
        if provider_base_url_value and not merged_config.get("base_url"):
            merged_config["base_url"] = provider_base_url_value

        return merged_config

    def _image_generation_config_for_tools(self, agent_cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        image_generation_cfg = agent_cfg.get("image_generation")
        if not isinstance(image_generation_cfg, dict):
            return image_generation_cfg

        image_generation_provider = normalize_image_generation_provider(image_generation_cfg.get("provider"))
        if image_generation_provider not in {IMAGE_GENERATION_PROVIDER_MINIMAX, IMAGE_GENERATION_PROVIDER_QWEN}:
            return image_generation_cfg

        configured_key = str(image_generation_cfg.get("api_key") or "").strip()
        merged_config = dict(image_generation_cfg)

        provider_cfg = agent_cfg.get("provider") or {}
        if not isinstance(provider_cfg, dict):
            return image_generation_cfg

        provider_is_native = (
            image_generation_provider == IMAGE_GENERATION_PROVIDER_MINIMAX and self._is_minimax_provider(provider_cfg)
        ) or (
            image_generation_provider == IMAGE_GENERATION_PROVIDER_QWEN and self._is_qwen_provider(provider_cfg)
        )
        if not provider_is_native:
            return image_generation_cfg

        if not configured_key or is_placeholder_api_key(configured_key):
            provider_key = str(provider_cfg.get("api_key") or "").strip()
            if not is_placeholder_api_key(provider_key):
                merged_config["api_key"] = provider_key

        if image_generation_provider == IMAGE_GENERATION_PROVIDER_QWEN:
            provider_base_url = str(provider_cfg.get("base_url") or "").strip()
            if provider_base_url and not merged_config.get("base_url") and not merged_config.get("endpoint"):
                merged_config["base_url"] = provider_base_url

        return merged_config

    def _initialize_message_storage(self) -> MessageStorage:
        """
        Initialize the default message storage backend.

        Subclasses can override `_create_message_storage` to plug in a different
        implementation while keeping the runner lifecycle unchanged.
        """
        return self._create_message_storage()

    def _get_message_storage_path(self) -> Path:
        return self.workspace / BaseAgentConfig.MESSAGE_DIRNAME / BaseAgentConfig.MESSAGE_DB_FILENAME

    def _create_message_storage(self) -> MessageStorage:
        return MessageStorage(path=str(self._get_message_storage_path()))

    def _initialize_skills_storage(self) -> SkillsStorageBase:
        return SkillsStorageLocal(self.skills_dir)
