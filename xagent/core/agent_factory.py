"""The only composition root for one Agent."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic

from ..components import MarkdownMemory, MessageStorage
from ..components.skills import SkillsStorageLocal
from ..integrations.langfuse import create_observability_runtime
from ..settings import XAgentSettings
from ..tools import (
    create_attach_artifact_tool,
    create_image_generation_tool,
    create_read_skill_tool,
    create_search_memory_tool,
    create_web_search_tool,
    create_workspace_run_command_tool,
    create_write_memory_tool,
)
from ..tools.image_generation_tool import IMAGE_GENERATION_PROVIDER_OPENAI
from ..tools.search_tool import (
    DEFAULT_QWEN_SEARCH_MODEL,
    SEARCH_PROVIDER_OPENAI,
    SEARCH_PROVIDER_QWEN,
    is_placeholder_api_key,
)
from .agent import Agent, AgentDependencies
from .config import AgentConfig
from .handlers import MemoryHandler, MessageHandler, ModelClient
from .journal import JournalLLMService
from .providers import (
    PROVIDER_QWEN,
    model_api_uses_anthropic_client,
    normalize_reasoning_config,
    provider_base_url,
    provider_is_official_openai,
    provider_model_api,
    provider_supports_vision,
    resolved_provider_name,
)
from .tooling import ToolExecutor, ToolManager


class AgentPaths:
    """Stable names for one self-contained Agent directory."""

    DEFAULT_CONFIG_DIR = AgentConfig.DEFAULT_WORKSPACE
    MEMORY_DIRNAME = AgentConfig.MEMORY_DIRNAME
    WORKSPACE_DIRNAME = AgentConfig.WORKSPACE_DIRNAME
    SKILLS_DIRNAME = AgentConfig.SKILLS_DIRNAME
    STATE_DB_FILENAME = "state.sqlite3"
    CONFIG_FILENAME = "config.yaml"
    IDENTITY_FILENAME = "identity.md"
    DEFAULT_HOST = "127.0.0.1"
    DEFAULT_PORT = 8010


class AgentFactory:
    """Load validated settings and wire dependencies exactly once."""

    def __init__(self, config_dir: str | Path | None = None) -> None:
        self.logger = logging.getLogger(type(self).__name__)
        self.config_dir = Path(
            config_dir or AgentPaths.DEFAULT_CONFIG_DIR
        ).expanduser().resolve()
        self.config_path = self.config_dir / AgentPaths.CONFIG_FILENAME
        self.identity_path = self.config_dir / AgentPaths.IDENTITY_FILENAME

        self.settings = XAgentSettings.load(self.config_path)
        self.config = self.settings.model_dump(mode="python", exclude_none=True)
        self.identity = self._read_identity()

        self.workspace_dir = self.config_dir / AgentPaths.WORKSPACE_DIRNAME
        self.skills_dir = self.config_dir / AgentPaths.SKILLS_DIRNAME
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir.mkdir(parents=True, exist_ok=True)

        observability_config = (
            self.settings.observability.model_dump(mode="python")
            if self.settings.observability is not None
            else None
        )
        self.observability = create_observability_runtime(observability_config)
        self.message_storage = MessageStorage(
            path=str(self.config_dir / AgentPaths.STATE_DB_FILENAME)
        )
        self.skills_storage = SkillsStorageLocal(self.skills_dir)
        self.agent = self._build_agent()

    def _read_identity(self) -> str:
        if not self.identity_path.is_file():
            raise FileNotFoundError(f"Identity file not found: {self.identity_path}")
        identity = self.identity_path.read_text(encoding="utf-8").strip()
        if not identity:
            raise ValueError(f"Identity file is empty: {self.identity_path}")
        return identity

    def _provider_data(self) -> dict[str, Any]:
        return self.settings.provider.model_dump(mode="python", exclude_none=True)

    def _build_agent(self) -> Agent:
        provider = self._provider_data()
        provider_name = resolved_provider_name(provider)
        model_api = provider_model_api(provider)
        reasoning = normalize_reasoning_config(provider)
        client = self._create_model_client(provider, model_api)
        tools = self._create_tools(client)

        markdown_memory = MarkdownMemory(
            memory_dir=str(self.config_dir / AgentPaths.MEMORY_DIRNAME)
        )
        journal = JournalLLMService(
            client=client,
            model=self.settings.provider.model,
            provider_name=provider_name,
            model_api=model_api,
            max_tokens=self.settings.provider.max_tokens,
            reasoning=reasoning,
        )
        memory_handler = MemoryHandler(
            memory=markdown_memory,
            llm_service=journal,
            message_storage=self.message_storage,
            max_history=self.settings.agent.max_history,
            recent_days=self.settings.agent.memory_recent_days,
        )
        tools.extend(
            (
                create_write_memory_tool(memory=markdown_memory, is_enabled=True),
                create_search_memory_tool(
                    memory=markdown_memory,
                    is_enabled=True,
                    message_storage=self.message_storage,
                ),
            )
        )

        tool_manager = ToolManager(tools=tools)
        message_handler = MessageHandler(
            message_storage=self.message_storage,
            system_prompt=self.identity,
            workspace_dir=self.workspace_dir,
        )
        model_client = ModelClient(
            client=client,
            model=self.settings.provider.model,
            provider_name=provider_name,
            model_api=model_api,
            max_tokens=self.settings.provider.max_tokens,
            reasoning=reasoning,
        )
        tool_executor = ToolExecutor(
            tool_manager=tool_manager,
            message_storage=self.message_storage,
            client=client,
            timeout_seconds=self.settings.runtime.tool_timeout_seconds,
        )
        return Agent(
            identity=self.identity,
            model=self.settings.provider.model,
            provider_name=provider_name,
            model_api=model_api,
            model_max_tokens=self.settings.provider.max_tokens,
            reasoning=reasoning,
            dependencies=AgentDependencies(
                client=client,
                message_storage=self.message_storage,
                markdown_memory=markdown_memory,
                llm_service=journal,
                memory_handler=memory_handler,
                message_handler=message_handler,
                tool_manager=tool_manager,
                model_client=model_client,
                tool_executor=tool_executor,
                workspace_dir=self.workspace_dir,
                skills_storage=self.skills_storage,
                observability=self.observability,
            ),
            supports_vision=provider_supports_vision(provider),
            max_history=self.settings.agent.max_history,
            max_iter=self.settings.agent.max_iter,
            subconscious_activity=self.settings.agent.subconscious_activity,
            memory_recent_days=self.settings.agent.memory_recent_days,
        )

    def _create_model_client(self, provider: dict[str, Any], model_api: str) -> Any:
        kwargs = {
            key: value
            for key, value in {
                "base_url": provider.get("base_url"),
                "api_key": provider.get("api_key"),
            }.items()
            if value
        }
        if model_api_uses_anthropic_client(model_api):
            return AsyncAnthropic(**kwargs)
        return self.observability.create_client(kwargs)

    def _create_tools(self, model_client: Any) -> list[Any]:
        tools: list[Any] = [
            create_attach_artifact_tool(workspace_dir=str(self.workspace_dir))
        ]
        if self.settings.tools.shell.enabled:
            tools.append(
                create_workspace_run_command_tool(
                    default_working_directory=str(self.workspace_dir)
                )
            )

        search_config = self._feature_config("search")
        search_tool = create_web_search_tool(
            search_config,
            client=self._search_client(search_config, model_client),
            model=self._search_model(search_config),
        )
        if search_tool is not None:
            tools.append(search_tool)

        image_config = self._feature_config("image_generation")
        image_tool = create_image_generation_tool(
            image_config,
            client=self._image_client(image_config, model_client),
            workspace_dir=str(self.workspace_dir),
        )
        if image_tool is not None:
            tools.append(image_tool)

        tools.append(create_read_skill_tool(self.skills_storage))
        return tools

    def _feature_config(self, section: str) -> dict[str, Any]:
        settings = getattr(self.settings, section)
        config = settings.model_dump(mode="python", exclude_none=True)
        provider = self._provider_data()
        if config.get("provider") != provider.get("name"):
            return config
        if is_placeholder_api_key(str(config.get("api_key") or "")):
            config["api_key"] = provider.get("api_key", "")
        if not config.get("base_url") and provider.get("base_url"):
            config["base_url"] = provider["base_url"]
        return config

    def _search_client(
        self,
        config: dict[str, Any],
        model_client: Any,
    ) -> Any:
        search_provider = config.get("provider")
        provider = self._provider_data()
        if search_provider == SEARCH_PROVIDER_OPENAI:
            return self._openai_feature_client(config, provider, model_client)
        if search_provider == SEARCH_PROVIDER_QWEN:
            key = str(config.get("api_key") or "")
            if not is_placeholder_api_key(key):
                return self.observability.create_client(
                    {
                        "api_key": key,
                        "base_url": config.get("base_url")
                        or provider_base_url(PROVIDER_QWEN),
                    }
                )
            if provider.get("name") == PROVIDER_QWEN:
                return model_client
            return None
        return model_client

    def _image_client(
        self,
        config: dict[str, Any],
        model_client: Any,
    ) -> Any:
        if config.get("provider") != IMAGE_GENERATION_PROVIDER_OPENAI:
            return model_client
        return self._openai_feature_client(
            config,
            self._provider_data(),
            model_client,
        )

    def _openai_feature_client(
        self,
        config: dict[str, Any],
        provider: dict[str, Any],
        model_client: Any,
    ) -> Any:
        key = str(config.get("api_key") or "")
        if not is_placeholder_api_key(key):
            return self.observability.create_client({"api_key": key})
        return model_client if provider_is_official_openai(provider) else None

    def _search_model(self, config: dict[str, Any]) -> str:
        if config.get("model"):
            return str(config["model"])
        provider = self._provider_data()
        if (
            config.get("provider") == SEARCH_PROVIDER_QWEN
            and provider.get("name") != PROVIDER_QWEN
        ):
            return DEFAULT_QWEN_SEARCH_MODEL
        return self.settings.provider.model
