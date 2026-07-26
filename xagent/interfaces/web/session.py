"""Selected-agent state for the local Web control client.

The browser chooses one registered agent and talks only to that agent's local
Runtime control service. Selection is process-local and never changes the CLI's
active-agent setting.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from pydantic import ValidationError

from ...core.agent_factory import AgentPaths
from ..cli.agents import (
    AgentRegistryError,
    agent_directory_has_contents,
    create_managed_agent,
    default_agent_dir,
    delete_managed_agent,
    load_agent_registry_or_empty,
    validate_agent_name,
)
from ..cli.setup import (
    ChannelSetupError,
    SETUP_CHANNELS,
    apply_channel_setup,
    build_channel_setup_schema,
    build_setup_schema,
    init_selection_from_mapping,
)
from ..cli.channels import load_config_file
from ...core.runtime import RuntimeClient, RuntimeUnavailable
from ...settings import XAgentSettings


SECRET_SENTINEL = "••••••••"


def _is_secret_field(name: str) -> bool:
    normalized = name.lower()
    return (
        normalized == "api_key"
        or normalized.endswith("_api_key")
        or normalized in {"app_secret", "secret_key", "password", "access_token"}
        or normalized.endswith("_secret_key")
    )


def _redact_secrets(value: Any, *, field_name: str = "") -> Any:
    if _is_secret_field(field_name):
        return SECRET_SENTINEL if value else value
    if isinstance(value, dict):
        return {
            str(key): _redact_secrets(item, field_name=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    return value


def _restore_secrets(submitted: Any, current: Any, *, field_name: str = "") -> Any:
    if _is_secret_field(field_name) and submitted == SECRET_SENTINEL:
        return current
    if isinstance(submitted, dict):
        current_mapping = current if isinstance(current, dict) else {}
        return {
            str(key): _restore_secrets(
                item,
                current_mapping.get(key),
                field_name=str(key),
            )
            for key, item in submitted.items()
        }
    if isinstance(submitted, list):
        current_list = current if isinstance(current, list) else []
        return [
            _restore_secrets(
                item,
                current_list[index] if index < len(current_list) else None,
            )
            for index, item in enumerate(submitted)
        ]
    return submitted


def _is_agent_initialized(path: Path) -> bool:
    config_file = path / AgentPaths.CONFIG_FILENAME
    identity_file = path / AgentPaths.IDENTITY_FILENAME
    if not config_file.is_file() or not identity_file.is_file():
        return False
    try:
        XAgentSettings.load(config_file)
        return bool(identity_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False


class WebAgentSession:
    """Tracks which registered agent the running web client is currently serving."""

    def __init__(
        self,
        *,
        initial_config_dir: Path,
        initial_agent_name: Optional[str] = None,
        registry_root: Optional[Path] = None,
    ) -> None:
        self._registry_root = registry_root
        self._initial_config_dir = Path(initial_config_dir).expanduser().resolve()
        self._current_name: Optional[str] = initial_agent_name

    def _load_registry(self):
        return load_agent_registry_or_empty(root=self._registry_root)

    @property
    def current_agent_name(self) -> Optional[str]:
        return self._current_name

    def list_agents(self) -> List[Dict[str, Any]]:
        registry = self._load_registry()
        current = self._resolve_agent_name()
        self._current_name = current
        rows: List[Dict[str, Any]] = []
        for name, entry in sorted(registry.agents.items()):
            try:
                runtime_status = RuntimeClient(entry.path).status()
            except RuntimeUnavailable:
                runtime_status = None
            try:
                settings = XAgentSettings.load(entry.path / AgentPaths.CONFIG_FILENAME)
                provider = settings.provider.name
                model = settings.provider.model
            except (OSError, ValueError):
                provider = ""
                model = ""
            rows.append({
                "name": name,
                "title": entry.title,
                "path": str(entry.path),
                "active": name == registry.active_agent,
                "selected": name == current,
                "initialized": _is_agent_initialized(entry.path),
                "runtime_running": bool(runtime_status and runtime_status.get("running")),
                "pid": int(runtime_status["pid"]) if runtime_status else None,
                "provider": provider,
                "model": model,
            })
        return rows

    def snapshot(self) -> Dict[str, Any]:
        registry = self._load_registry()
        selected = self._resolve_agent_name() or ""
        self._current_name = selected or None
        return {
            "active_agent": registry.active_agent,
            "selected_agent": selected,
            "agents": self.list_agents(),
        }

    def select(self, name: str) -> Dict[str, Any]:
        registry = self._load_registry()
        normalized = validate_agent_name(name)
        if normalized not in registry.agents:
            raise AgentRegistryError(
                f"Unknown agent {normalized!r}."
            )
        self._current_name = normalized
        return self.snapshot()

    def check_name_availability(self, name: str) -> Dict[str, Any]:
        normalized = validate_agent_name(name)
        registry = self._load_registry()
        path = default_agent_dir(normalized, root=self._registry_root)
        return {
            "name": normalized,
            "registered": normalized in registry.agents,
            "directory_exists": agent_directory_has_contents(path),
            "path": str(path),
        }

    def setup_schema(self) -> Dict[str, Any]:
        return build_setup_schema()

    def create_agent(
        self,
        *,
        name: str,
        title: Optional[str] = None,
        replace_existing: bool = False,
        selection_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        selection = init_selection_from_mapping(selection_data)
        create_managed_agent(
            name,
            selection=selection,
            title=title,
            replace_existing=replace_existing,
            make_active=True,
            root=self._registry_root,
        )
        self._current_name = validate_agent_name(name)
        return self.snapshot()

    def delete_agent(self, name: str, *, confirm: str) -> Dict[str, Any]:
        normalized = validate_agent_name(name)
        if confirm != normalized:
            raise AgentRegistryError("Confirmation name does not match the agent to delete.")
        delete_managed_agent(normalized, root=self._registry_root, stop_runtime=True)
        registry = self._load_registry()
        if self._current_name == normalized:
            self._current_name = registry.active_agent or None
        return self.snapshot()

    def _entry_path(self, name: str) -> Path:
        registry = self._load_registry()
        entry = registry.agents.get(name)
        if entry is None:
            raise AgentRegistryError(f"Unknown agent {name!r}.")
        return entry.path

    def _no_agents_http_error(self) -> HTTPException:
        return HTTPException(
            status_code=404,
            detail="No agents are registered. Create an agent to use this feature.",
        )

    def _resolve_agent_name(self) -> Optional[str]:
        registry = self._load_registry()
        if not registry.agents:
            return None
        if self._current_name and self._current_name in registry.agents:
            return self._current_name
        if registry.active_agent and registry.active_agent in registry.agents:
            return registry.active_agent
        return next(iter(sorted(registry.agents)))

    def get_current_config_dir(self) -> Path:
        name = self._resolve_agent_name()
        if name is None:
            if (self._initial_config_dir / AgentPaths.CONFIG_FILENAME).is_file():
                return self._initial_config_dir
            raise self._no_agents_http_error()
        return self._entry_path(name).expanduser().resolve()

    def settings_snapshot(self) -> Dict[str, Any]:
        settings = XAgentSettings.load(
            self.get_current_config_dir() / AgentPaths.CONFIG_FILENAME
        )
        return {
            "settings": _redact_secrets(
                settings.model_dump(mode="json", exclude_none=True)
            ),
            "schema": XAgentSettings.json_schema(),
            "secret_sentinel": SECRET_SENTINEL,
        }

    def update_settings(self, submitted: Dict[str, Any]) -> Dict[str, Any]:
        config_path = self.get_current_config_dir() / AgentPaths.CONFIG_FILENAME
        current = XAgentSettings.load(config_path)
        current_data = current.model_dump(mode="python", exclude_none=True)
        restored = _restore_secrets(submitted, current_data)
        try:
            updated = XAgentSettings.model_validate(restored)
        except ValidationError as exc:
            errors = []
            for error in exc.errors(include_input=False, include_url=False):
                location = ".".join(str(item) for item in error["loc"])
                errors.append(f"{location}: {error['msg']}")
            raise ValueError("Invalid configuration: " + "; ".join(errors)) from exc
        updated.write_atomic(config_path)
        return self.settings_snapshot()

    def channel_setup_schema(self, channel: str) -> Dict[str, Any]:
        normalized = str(channel or "").strip().lower()
        if normalized not in SETUP_CHANNELS:
            raise HTTPException(status_code=404, detail=f"Unknown channel: {channel}")
        config_dir = self.get_current_config_dir()
        config = load_config_file(config_dir)
        return build_channel_setup_schema(normalized, config)

    def apply_channel_setup(
        self,
        channel: str,
        *,
        selection_data: Dict[str, Any],
        force: bool = False,
    ) -> Dict[str, Any]:
        normalized = str(channel or "").strip().lower()
        if normalized not in SETUP_CHANNELS:
            raise HTTPException(status_code=404, detail=f"Unknown channel: {channel}")
        config_dir = self.get_current_config_dir()
        try:
            result = apply_channel_setup(
                channel=normalized,
                config_dir=config_dir,
                selection_data=selection_data,
                force=force,
            )
        except ChannelSetupError as exc:
            message = str(exc)
            status_code = 409 if "already exists" in message else 400
            raise HTTPException(status_code=status_code, detail=message) from exc
        return result
