"""Agent registry and CLI management commands."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from ...core.agent_factory import AgentPaths
from ...settings import write_text_atomic


REGISTRY_FILENAME = "agents.yaml"
AGENTS_DIRNAME = "agents"
DEFAULT_AGENT_NAME = "default"
REGISTRY_VERSION = 1

_AGENT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")


class AgentRegistryError(ValueError):
    """Raised when the agent registry cannot satisfy a request."""


@dataclass(frozen=True)
class AgentEntry:
    """One managed agent entry."""

    name: str
    title: str
    path: Path

    def to_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "path": str(self.path),
        }


@dataclass(frozen=True)
class AgentRegistry:
    """Persisted list of managed agents."""

    active_agent: str
    agents: dict[str, AgentEntry]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": REGISTRY_VERSION,
            "active_agent": self.active_agent,
            "agents": {
                name: entry.to_dict()
                for name, entry in sorted(self.agents.items())
            },
        }


def management_root() -> Path:
    return Path(AgentPaths.DEFAULT_CONFIG_DIR).expanduser().resolve()


def registry_path(*, root: Optional[Path] = None) -> Path:
    return (root or management_root()) / REGISTRY_FILENAME


def default_agent_dir(name: str, *, root: Optional[Path] = None) -> Path:
    validate_agent_name(name)
    return (root or management_root()) / AGENTS_DIRNAME / name


def validate_agent_name(name: str) -> str:
    normalized = str(name or "").strip()
    if not _AGENT_NAME_PATTERN.fullmatch(normalized):
        raise AgentRegistryError(
            "Agent name must start with a lowercase letter and contain only lowercase letters, "
            "digits, hyphens, or underscores."
        )
    if normalized in {".", ".."}:
        raise AgentRegistryError("Agent name cannot be a relative path marker.")
    return normalized


def _default_title(name: str) -> str:
    return " ".join(part.capitalize() for part in name.replace("_", "-").split("-") if part) or name


def _expand_entry_path(raw_path: Any, *, root: Path) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise AgentRegistryError("Agent path must be a non-empty string.")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _load_registry_data(path: Path) -> Mapping[str, Any]:
    if not path.is_file():
        raise AgentRegistryError(
            f"Agent registry not found: {path}. Run `xagent setup`."
        )
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, Mapping):
        raise AgentRegistryError(f"Agent registry must be a mapping: {path}")
    return data


def load_agent_registry(*, root: Optional[Path] = None) -> AgentRegistry:
    root_path = root or management_root()
    path = registry_path(root=root_path)
    data = _load_registry_data(path)

    agents_data = data.get("agents")
    if not isinstance(agents_data, Mapping) or not agents_data:
        raise AgentRegistryError(f"Agent registry has no agents: {path}")

    entries: dict[str, AgentEntry] = {}
    for raw_name, raw_entry in agents_data.items():
        name = validate_agent_name(str(raw_name))
        if not isinstance(raw_entry, Mapping):
            raise AgentRegistryError(f"Agent entry {name!r} must be a mapping.")
        title = str(raw_entry.get("title") or _default_title(name)).strip() or _default_title(name)
        entry_path = _expand_entry_path(raw_entry.get("path"), root=root_path)
        entries[name] = AgentEntry(name=name, title=title, path=entry_path)

    active_agent = str(data.get("active_agent") or "").strip()
    if not active_agent:
        active_agent = next(iter(sorted(entries)))
    active_agent = validate_agent_name(active_agent)
    if active_agent not in entries:
        raise AgentRegistryError(f"Active agent {active_agent!r} is not registered.")

    return AgentRegistry(active_agent=active_agent, agents=entries)


def save_agent_registry(registry: AgentRegistry, *, root: Optional[Path] = None) -> None:
    root_path = root or management_root()
    path = registry_path(root=root_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(
        path,
        yaml.safe_dump(
            registry.to_dict(),
            sort_keys=False,
            allow_unicode=False,
        ),
    )


def empty_agent_registry(*, active_agent: str = "") -> AgentRegistry:
    return AgentRegistry(active_agent=active_agent, agents={})


def allocate_api_port(*, root: Optional[Path] = None) -> int:
    """Return the lowest unused API port at or above the default.

    Reads every registered agent's config.yaml and collects existing
    ``channels.api.port`` values, then returns the first port >=
    ``DEFAULT_PORT`` (8010) not in that set.
    """
    from .channels import api_config, load_config_file

    used_ports: set[int] = set()
    try:
        registry = load_agent_registry(root=root)
    except AgentRegistryError:
        return AgentPaths.DEFAULT_PORT

    for entry in registry.agents.values():
        try:
            cfg = load_config_file(entry.path)
        except Exception:
            continue
        port_value = api_config(cfg).get("port")
        if isinstance(port_value, int):
            used_ports.add(port_value)

    port = AgentPaths.DEFAULT_PORT
    while port in used_ports:
        port += 1
    return port


def load_agent_registry_or_empty(*, root: Optional[Path] = None) -> AgentRegistry:
    root_path = root or management_root()
    path = registry_path(root=root_path)
    if not path.is_file():
        return empty_agent_registry()
    try:
        return load_agent_registry(root=root_path)
    except AgentRegistryError as exc:
        if "has no agents" not in str(exc):
            raise
    return empty_agent_registry()


def resolve_agent_name(agent_name: Optional[str] = None, *, root: Optional[Path] = None) -> str:
    registry = load_agent_registry(root=root)
    if agent_name:
        name = validate_agent_name(agent_name)
        if name not in registry.agents:
            raise AgentRegistryError(f"Unknown agent {name!r}.")
        return name
    return registry.active_agent


def resolve_agent_runtime_dir(agent_name: Optional[str] = None, *, root: Optional[Path] = None) -> Path:
    registry = load_agent_registry(root=root)
    name = validate_agent_name(agent_name) if agent_name else registry.active_agent
    entry = registry.agents.get(name)
    if entry is None:
        raise AgentRegistryError(f"Unknown agent {name!r}.")
    return entry.path


def register_agent(
    name: str,
    *,
    path: Optional[Path] = None,
    title: Optional[str] = None,
    make_active: bool = False,
    root: Optional[Path] = None,
) -> AgentRegistry:
    root_path = root or management_root()
    normalized = validate_agent_name(name)
    registry = load_agent_registry_or_empty(root=root_path)
    if normalized in registry.agents:
        raise AgentRegistryError(f"Agent {normalized!r} is already registered.")
    entry = AgentEntry(
        name=normalized,
        title=(title or _default_title(normalized)).strip() or _default_title(normalized),
        path=(path or default_agent_dir(normalized, root=root_path)).expanduser().resolve(),
    )
    agents = dict(registry.agents)
    agents[normalized] = entry
    active_agent = normalized if make_active or not registry.active_agent else registry.active_agent
    updated = AgentRegistry(active_agent=active_agent, agents=agents)
    save_agent_registry(updated, root=root_path)
    return updated


def select_agent(name: str, *, root: Optional[Path] = None) -> AgentRegistry:
    root_path = root or management_root()
    registry = load_agent_registry(root=root_path)
    normalized = validate_agent_name(name)
    if normalized not in registry.agents:
        raise AgentRegistryError(f"Unknown agent {normalized!r}.")
    updated = AgentRegistry(active_agent=normalized, agents=dict(registry.agents))
    save_agent_registry(updated, root=root_path)
    return updated


def remove_agent(name: str, *, root: Optional[Path] = None) -> tuple[AgentRegistry, AgentEntry]:
    root_path = root or management_root()
    registry = load_agent_registry(root=root_path)
    normalized = validate_agent_name(name)
    if normalized not in registry.agents:
        raise AgentRegistryError(f"Unknown agent {normalized!r}.")
    agents = dict(registry.agents)
    removed = agents.pop(normalized)
    active_agent = registry.active_agent
    if active_agent == normalized:
        active_agent = next(iter(sorted(agents)), "")
    updated = AgentRegistry(active_agent=active_agent, agents=agents)
    save_agent_registry(updated, root=root_path)
    return updated, removed


def _is_managed_agent_path(path: Path, *, root: Optional[Path] = None) -> bool:
    root_path = root or management_root()
    managed_root = (root_path / AGENTS_DIRNAME).resolve()
    resolved_path = path.expanduser().resolve()
    try:
        resolved_path.relative_to(managed_root)
    except ValueError:
        return False
    return resolved_path != managed_root


def delete_agent_directory(path: Path, *, root: Optional[Path] = None) -> bool:
    """Delete a managed agent directory if it exists."""
    resolved_path = path.expanduser().resolve()
    if not resolved_path.exists():
        return False
    if not resolved_path.is_dir():
        raise AgentRegistryError(f"Agent path is not a directory: {resolved_path}")
    if not _is_managed_agent_path(resolved_path, root=root):
        raise AgentRegistryError(f"Refusing to delete unmanaged agent path: {resolved_path}")
    shutil.rmtree(resolved_path)
    return True


def _directory_has_contents(path: Path) -> bool:
    return path.exists() and path.is_dir() and any(path.iterdir())


def agent_directory_has_contents(path: Path) -> bool:
    return _directory_has_contents(path)


def create_managed_agent(
    name: str,
    *,
    selection: Any,
    title: Optional[str] = None,
    replace_existing: bool = False,
    make_active: bool = True,
    root: Optional[Path] = None,
) -> AgentRegistry:
    """Create a managed agent directory, register it, and optionally make it active."""
    from .setup import InitSelection, init_agent_directory

    if not isinstance(selection, InitSelection):
        raise AgentRegistryError("Agent setup selection is invalid.")

    root_path = root or management_root()
    normalized = validate_agent_name(name)
    path = default_agent_dir(normalized, root=root_path)
    registry = load_agent_registry_or_empty(root=root_path)
    if normalized in registry.agents:
        raise AgentRegistryError(f"Agent {normalized!r} is already registered.")
    if _directory_has_contents(path):
        if not replace_existing:
            raise AgentRegistryError(
                f"Agent directory already exists at {path}. "
                "Set replace_existing to true to replace it."
            )
        delete_agent_directory(path, root=root_path)

    result = init_agent_directory(
        str(path),
        force=False,
        selection=selection,
        quiet=True,
        registry_root=root_path,
    )
    if not result.wrote_files:
        raise AgentRegistryError("Failed to initialize agent directory.")

    register_agent(
        normalized,
        path=path,
        title=title,
        make_active=make_active and not registry.agents,
        root=root_path,
    )
    if make_active:
        return select_agent(normalized, root=root_path)
    return load_agent_registry(root=root_path)


def delete_managed_agent(
    name: str,
    *,
    root: Optional[Path] = None,
    stop_runtime: bool = True,
) -> tuple[AgentRegistry, AgentEntry]:
    """Remove a managed agent from the registry and delete its data directory."""
    from ...core.runtime import RuntimeLaunchError, RuntimeLauncher

    root_path = root or management_root()
    registry = load_agent_registry(root=root_path)
    normalized = validate_agent_name(name)
    entry = registry.agents.get(normalized)
    if entry is None:
        raise AgentRegistryError(f"Unknown agent {normalized!r}.")

    if stop_runtime:
        try:
            RuntimeLauncher(entry.path).stop()
        except RuntimeLaunchError as exc:
            raise AgentRegistryError(f"Failed to stop the Agent Runtime: {exc}") from exc

    delete_agent_directory(entry.path, root=root_path)
    return remove_agent(normalized, root=root_path)


def ensure_default_agent_for_setup(agent_name: Optional[str] = None) -> Path:
    """Resolve setup target, creating the default registry on first use."""
    if agent_name:
        return resolve_agent_runtime_dir(agent_name)
    try:
        return resolve_agent_runtime_dir(None)
    except AgentRegistryError as exc:
        message = str(exc)
        if "not found" not in message and "has no agents" not in message:
            raise
    registry = register_agent(
        DEFAULT_AGENT_NAME,
        path=default_agent_dir(DEFAULT_AGENT_NAME),
        make_active=True,
    )
    return registry.agents[DEFAULT_AGENT_NAME].path
