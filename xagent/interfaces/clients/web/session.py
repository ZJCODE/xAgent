"""Multi-agent session state for the built-in web client.

The web client can list every agent registered with the CLI (``agents.yaml``)
and switch which one's Chat/Memory/Message/Workspace/Skills/Tasks/Agent data
is currently being served, similar to the interactive launcher's Agents menu.

The "currently selected agent" lives in-memory on the running web client
process only (shared by every browser tab pointed at it) and is never written
back to ``agents.yaml`` — switching in the browser never changes the CLI's
own notion of the active agent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from ...base import BaseAgentConfig
from ...cli.agents import (
    AgentRegistryError,
    agent_directory_has_contents,
    create_managed_agent,
    default_agent_dir,
    delete_managed_agent,
    load_agent_registry_or_empty,
    validate_agent_name,
)
from ...cli.setup import (
    ChannelSetupError,
    SETUP_CHANNELS,
    apply_channel_setup,
    build_channel_setup_schema,
    build_setup_schema,
    init_selection_from_mapping,
)
from ...cli.channels import CHANNEL_API, load_config_file
from ...cli.clients import web_client_config
from ...cli.processes import managed_paths, running_pid
from ...server.admin_service import AdminService


def _is_agent_initialized(path: Path) -> bool:
    config_file = path / BaseAgentConfig.CONFIG_FILENAME
    identity_file = path / BaseAgentConfig.IDENTITY_FILENAME
    if not config_file.is_file() or not identity_file.is_file():
        return False
    try:
        return bool(identity_file.read_text(encoding="utf-8", errors="replace").strip())
    except OSError:
        return False


def _safe_load_config(path: Path) -> Dict[str, Any]:
    try:
        return load_config_file(path)
    except Exception:
        return {}


class WebAgentSession:
    """Tracks which registered agent the running web client is currently serving."""

    def __init__(
        self,
        *,
        initial_config_dir: Path,
        initial_agent_name: Optional[str] = None,
        initial_api_url: str = "",
        registry_root: Optional[Path] = None,
    ) -> None:
        self._registry_root = registry_root
        self._initial_config_dir = Path(initial_config_dir).expanduser().resolve()
        self._initial_agent_name = initial_agent_name
        self._initial_api_url = initial_api_url
        self._current_name: Optional[str] = initial_agent_name
        self._admin_cache: Dict[str, AdminService] = {}

    def _load_registry(self):
        return load_agent_registry_or_empty(root=self._registry_root)

    @property
    def current_agent_name(self) -> Optional[str]:
        return self._current_name

    def list_agents(self) -> List[Dict[str, Any]]:
        registry = self._load_registry()
        current = self._current_name
        rows: List[Dict[str, Any]] = []
        for name, entry in sorted(registry.agents.items()):
            pid = running_pid(managed_paths(entry.path, CHANNEL_API).pid_path)
            rows.append({
                "name": name,
                "title": entry.title,
                "path": str(entry.path),
                "active": name == registry.active_agent,
                "selected": name == current,
                "initialized": _is_agent_initialized(entry.path),
                "channel_running": pid is not None,
            })
        return rows

    def snapshot(self) -> Dict[str, Any]:
        registry = self._load_registry()
        return {
            "active_agent": registry.active_agent,
            "selected_agent": self._current_name or "",
            "agents": self.list_agents(),
        }

    def select(self, name: str) -> Dict[str, Any]:
        registry = self._load_registry()
        normalized = validate_agent_name(name)
        if normalized not in registry.agents:
            raise AgentRegistryError(
                f"Unknown agent {normalized!r}. Run `xagent agents list` to see available agents."
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
        delete_managed_agent(normalized, root=self._registry_root, stop_channels=True)
        self._admin_cache.pop(normalized, None)
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
            raise self._no_agents_http_error()
        return self._entry_path(name).expanduser().resolve()

    @staticmethod
    def _build_admin(config_dir: Path) -> AdminService:
        try:
            return AdminService(config_dir=str(config_dir))
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Agent configuration is missing or invalid at {config_dir}: {exc}",
            ) from exc

    def get_current_admin(self) -> AdminService:
        name = self._resolve_agent_name()
        if name is None:
            raise self._no_agents_http_error()
        cached = self._admin_cache.get(name)
        if cached is not None:
            return cached
        entry_path = self._entry_path(name)
        admin = self._build_admin(entry_path)
        self._admin_cache[name] = admin
        return admin

    def invalidate_admin_cache(self, name: Optional[str] = None) -> None:
        """Drop cached admin state so the next request reloads files from disk."""
        if name is None:
            name = self._resolve_agent_name()
        if name:
            self._admin_cache.pop(name, None)

    def get_current_api_url(self) -> str:
        name = self._resolve_agent_name()
        if name is None:
            return self._initial_api_url
        if name == self._initial_agent_name and self._initial_api_url:
            return self._initial_api_url
        entry_path = self._entry_path(name)
        cfg = _safe_load_config(entry_path)
        return web_client_config(cfg)["api_url"]

    def channel_setup_schema(self, channel: str) -> Dict[str, Any]:
        normalized = str(channel or "").strip().lower()
        if normalized not in SETUP_CHANNELS:
            raise HTTPException(status_code=404, detail=f"Unknown channel: {channel}")
        config_dir = self.get_current_config_dir()
        config = _safe_load_config(config_dir)
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
        self.invalidate_admin_cache()
        return result
