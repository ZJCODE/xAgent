"""Agent registry and CLI management commands."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from ..base import BaseAgentConfig


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
    return Path(BaseAgentConfig.DEFAULT_CONFIG_DIR).expanduser().resolve()


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
            f"Agent registry not found: {path}. Run `xagent agents create default` or open the launcher setup."
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
    path.write_text(yaml.safe_dump(registry.to_dict(), sort_keys=False, allow_unicode=False), encoding="utf-8")


def empty_agent_registry(*, active_agent: str = "") -> AgentRegistry:
    return AgentRegistry(active_agent=active_agent, agents={})


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
            raise AgentRegistryError(f"Unknown agent {name!r}. Run `xagent agents list` to see available agents.")
        return name
    return registry.active_agent


def resolve_agent_runtime_dir(agent_name: Optional[str] = None, *, root: Optional[Path] = None) -> Path:
    registry = load_agent_registry(root=root)
    name = validate_agent_name(agent_name) if agent_name else registry.active_agent
    entry = registry.agents.get(name)
    if entry is None:
        raise AgentRegistryError(f"Unknown agent {name!r}. Run `xagent agents list` to see available agents.")
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
        raise AgentRegistryError(f"Unknown agent {normalized!r}. Run `xagent agents list` to see available agents.")
    updated = AgentRegistry(active_agent=normalized, agents=dict(registry.agents))
    save_agent_registry(updated, root=root_path)
    return updated


def remove_agent(name: str, *, root: Optional[Path] = None) -> tuple[AgentRegistry, AgentEntry]:
    root_path = root or management_root()
    registry = load_agent_registry(root=root_path)
    normalized = validate_agent_name(name)
    if normalized not in registry.agents:
        raise AgentRegistryError(f"Unknown agent {normalized!r}. Run `xagent agents list` to see available agents.")
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


def _confirm_destructive_action(prompt: str, *, expected: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("Refusing to delete without confirmation. Re-run with --yes to confirm.")
        return False
    answer = input(f"{prompt}\nType {expected!r} to confirm: ").strip()
    return answer == expected


def _delete_confirmation_text(name: str, path: Path, *, action: str) -> str:
    return (
        f"{action} agent {name!r} and delete all data at:\n"
        f"{path}\n"
        "This removes config, identity, memory, messages, workspace, skills, tasks, logs, and run state."
    )


def agent_registry_rows(registry: AgentRegistry) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "title": entry.title,
            "path": str(entry.path),
            "active": name == registry.active_agent,
        }
        for name, entry in sorted(registry.agents.items())
    ]


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
        title="Default",
        make_active=True,
    )
    return registry.agents[DEFAULT_AGENT_NAME].path


def _agent_summary(entry: AgentEntry, *, active: bool) -> dict[str, Any]:
    config_file = entry.path / BaseAgentConfig.CONFIG_FILENAME
    identity_file = entry.path / BaseAgentConfig.IDENTITY_FILENAME
    initialized = config_file.is_file() and identity_file.is_file() and bool(
        identity_file.read_text(encoding="utf-8", errors="replace").strip()
        if identity_file.is_file()
        else ""
    )
    return {
        "name": entry.name,
        "title": entry.title,
        "path": str(entry.path),
        "active": active,
        "initialized": initialized,
        "config": str(config_file),
        "identity": str(identity_file),
    }


def _print_agent_error(exc: Exception) -> int:
    print(f"Error: {exc}")
    return 1


def handle_agents(args: argparse.Namespace) -> int:
    action = getattr(args, "agents_action", "")
    try:
        if action == "list":
            registry = load_agent_registry_or_empty()
            rows = agent_registry_rows(registry)
            if getattr(args, "json_output", False):
                print(json.dumps({"active_agent": registry.active_agent, "agents": rows}, indent=2, sort_keys=True))
                return 0
            if not rows:
                print("No agents are registered yet.")
                print("Create one with: xagent agents create default")
                return 0
            for row in rows:
                marker = "*" if row["active"] else " "
                print(f"{marker} {row['name']}  {row['title']}")
                print(f"  path: {row['path']}")
            return 0

        if action == "info":
            registry = load_agent_registry()
            name = validate_agent_name(args.name)
            entry = registry.agents.get(name)
            if entry is None:
                raise AgentRegistryError(f"Unknown agent {name!r}. Run `xagent agents list` to see available agents.")
            payload = _agent_summary(entry, active=name == registry.active_agent)
            if getattr(args, "json_output", False):
                print(json.dumps(payload, indent=2, sort_keys=True))
                return 0
            print(f"Agent: {payload['name']}")
            print(f"Title: {payload['title']}")
            print(f"Active: {payload['active']}")
            print(f"Initialized: {payload['initialized']}")
            print(f"Path: {payload['path']}")
            print(f"Config: {payload['config']}")
            print(f"Identity: {payload['identity']}")
            return 0

        if action == "select":
            registry = select_agent(args.name)
            entry = registry.agents[registry.active_agent]
            print(f"Active agent: {entry.name} ({entry.path})")
            return 0

        if action == "remove":
            registry = load_agent_registry()
            name = validate_agent_name(args.name)
            entry = registry.agents.get(name)
            if entry is None:
                raise AgentRegistryError(f"Unknown agent {name!r}. Run `xagent agents list` to see available agents.")
            if not _confirm_destructive_action(
                _delete_confirmation_text(name, entry.path, action="Remove"),
                expected=name,
                assume_yes=getattr(args, "yes", False),
            ):
                print("Remove cancelled.")
                return 1
            deleted = delete_agent_directory(entry.path)
            _registry, removed = remove_agent(name)
            print(f"Removed agent: {removed.name}")
            if deleted:
                print(f"Deleted data: {removed.path}")
            else:
                print(f"Data directory did not exist: {removed.path}")
            return 0

        if action == "create":
            name = validate_agent_name(args.name)
            path = default_agent_dir(name)
            registry = load_agent_registry_or_empty()
            if name in registry.agents:
                raise AgentRegistryError(f"Agent {name!r} is already registered.")
            if _directory_has_contents(path):
                if not _confirm_destructive_action(
                    _delete_confirmation_text(name, path, action="Replace existing directory for"),
                    expected=name,
                    assume_yes=getattr(args, "yes", False),
                ):
                    print("Create cancelled.")
                    return 1
                delete_agent_directory(path)
            from .setup import collect_init_selection_terminal_ui, init_agent_directory

            selection = collect_init_selection_terminal_ui()
            result = init_agent_directory(str(path), force=False, selection=selection)
            if not result.wrote_files:
                return 1
            updated = register_agent(
                name,
                path=path,
                title=getattr(args, "title", None) or _default_title(name),
                make_active=not registry.agents,
            )
            active_note = " active" if updated.active_agent == name else ""
            print(f"Created{active_note} agent {name}: {path}")
            return 0
    except (AgentRegistryError, OSError, yaml.YAMLError) as exc:
        return _print_agent_error(exc)

    print(f"Unknown agents action: {action}")
    return 1
