"""Service layer for the xAgent Web Console."""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from fastapi import HTTPException, UploadFile

from ..base import BaseAgentConfig
from ..cli import runtime as cli_runtime
from ..cli.agents import (
    AgentEntry,
    AgentRegistry,
    AgentRegistryError,
    default_agent_dir,
    delete_agent_directory,
    load_agent_registry,
    load_agent_registry_or_empty,
    register_agent,
    remove_agent,
    select_agent,
    validate_agent_name,
    allocate_api_port,
)
from ..cli.channels import (
    CHANNEL_API,
    CHANNEL_FEISHU,
    CHANNEL_VOICE,
    CHANNEL_WEIXIN,
    VALID_CHANNELS,
    api_config,
    enabled_channels_from_config,
    feishu_config,
    load_config_file,
    voice_config,
    weixin_config,
)
from ..cli.config_editor import validate_config, write_config
from ..cli.overview import RuntimeOverview, build_runtime_overview
from ..cli.processes import managed_paths, running_pid, stop_managed_process, tail_text
from ..cli.setup import (
    API_KEY_PLACEHOLDER,
    FeishuInitSelection,
    InitSelection,
    WeixinInitSelection,
    _ensure_api_port,
    _feishu_channel_config,
    _format_identity_markdown,
    _weixin_channel_config,
    init_agent_directory,
)
from ...components import MessageStorage, SkillsStorageLocal
from ...core.runtime import delete_scheduled_task, list_task_records
from ...schemas.attachment import (
    DEFAULT_WEB_ATTACHMENT_DIR,
    DEFAULT_WEB_IMAGE_DIR,
    MAX_ATTACHMENT_BYTES,
    safe_attachment_filename,
)
from ...utils.image_utils import (
    MAX_IMAGE_BYTES,
    SUPPORTED_UPLOAD_IMAGE_MIME_TYPES,
    detect_image_mime,
)
from .files import WorkspaceFileService
from .serializers import message_item, message_search_result


CONSOLE_PORT = BaseAgentConfig.DEFAULT_PORT
AGENT_API_PORT_START = BaseAgentConfig.DEFAULT_PORT + 1
SECRET_MASK = "********"
_SECRET_KEYS = {"api_key", "secret_key", "app_secret"}
_WORKSPACE_TEXT_READ_LIMIT = 1_000_000
_WORKSPACE_SEARCH_TEXT_LIMIT = 2_000_000


def console_http_url(host: str | None = None, port: int | None = None) -> str:
    host_value = host or BaseAgentConfig.DEFAULT_HOST
    port_value = port or CONSOLE_PORT
    return f"http://{host_value}:{port_value}"


def _registry_error(exc: Exception) -> HTTPException:
    if isinstance(exc, AgentRegistryError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


def _agent_config_path(entry: AgentEntry) -> Path:
    return entry.path / BaseAgentConfig.CONFIG_FILENAME


def _agent_identity_path(entry: AgentEntry) -> Path:
    return entry.path / BaseAgentConfig.IDENTITY_FILENAME


def _is_initialized(entry: AgentEntry) -> bool:
    config_path = _agent_config_path(entry)
    identity_path = _agent_identity_path(entry)
    try:
        return config_path.is_file() and identity_path.is_file() and bool(identity_path.read_text(encoding="utf-8").strip())
    except OSError:
        return False


def _status_tone(status: str) -> str:
    if status in {"ok", "running"}:
        return "ok"
    if status in {"warning", "idle", "stopped", "disabled"}:
        return "idle"
    return "error"


def _api_service_url(config: dict[str, Any]) -> str:
    cfg = api_config(config)
    host = str(cfg.get("host") or BaseAgentConfig.DEFAULT_HOST).strip() or BaseAgentConfig.DEFAULT_HOST
    port = int(cfg.get("port") or AGENT_API_PORT_START)
    browse_host = "127.0.0.1" if host == "0.0.0.0" else host
    if ":" in browse_host and not browse_host.startswith("["):
        browse_host = f"[{browse_host}]"
    return f"http://{browse_host}:{port}"


def _api_ws_url(config: dict[str, Any]) -> str:
    return _api_service_url(config).replace("http://", "ws://", 1).replace("https://", "wss://", 1)


def _redact_config(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key in _SECRET_KEYS:
                result[key] = SECRET_MASK if str(item or "").strip() else ""
            else:
                result[key] = _redact_config(item)
        return result
    if isinstance(value, list):
        return [_redact_config(item) for item in value]
    return value


def _restore_masked_secrets(candidate: Any, original: Any, key: str = "") -> Any:
    if key in _SECRET_KEYS and candidate == SECRET_MASK:
        return original
    if isinstance(candidate, dict) and isinstance(original, dict):
        result: dict[str, Any] = {}
        for item_key, item_value in candidate.items():
            result[item_key] = _restore_masked_secrets(item_value, original.get(item_key), item_key)
        return result
    if isinstance(candidate, list) and isinstance(original, list):
        return [
            _restore_masked_secrets(item, original[index] if index < len(original) else None, key)
            for index, item in enumerate(candidate)
        ]
    return candidate


def _overview_items(overview: RuntimeOverview) -> list[dict[str, str]]:
    return [
        {
            "name": item.name,
            "value": item.value,
            "status": item.status,
            "tone": _status_tone(item.status),
            "detail": item.detail,
        }
        for item in overview.items
    ]


def _missing_secret_paths(value: Any, prefix: str = "") -> list[str]:
    missing: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if key in _SECRET_KEYS and str(item or "").strip() in {"", API_KEY_PLACEHOLDER, "your_api_key_here"}:
                missing.append(path)
            missing.extend(_missing_secret_paths(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            missing.extend(_missing_secret_paths(item, f"{prefix}[{index}]"))
    return missing


def _content_addressed_upload_name(filename: str, content: bytes) -> str:
    import hashlib

    safe_name = safe_attachment_filename(filename)
    path = Path(safe_name)
    digest = hashlib.sha1(content).hexdigest()[:12]
    stem = path.stem or "upload"
    return f"{stem}-{digest}{path.suffix}"


def _channel_args(config_dir: Path, channel: str, **overrides: Any) -> argparse.Namespace:
    values: dict[str, Any] = {
        "agent": None,
        "config_dir": str(config_dir),
        "channels": [channel],
        "host": None,
        "port": None,
        "open_browser": False,
        "max_concurrent_chats": None,
        "queue_timeout": None,
        "chat_timeout": None,
        "user_id": "local_voice",
        "verbose": False,
        "input_device": None,
        "output_device": None,
        "lines": 80,
        "follow": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class AgentRegistryService:
    """Read and mutate the managed agent registry."""

    def registry(self) -> AgentRegistry:
        return load_agent_registry_or_empty()

    def require_registry(self) -> AgentRegistry:
        try:
            return load_agent_registry()
        except Exception as exc:
            raise _registry_error(exc) from exc

    def require_entry(self, name: str) -> tuple[AgentRegistry, AgentEntry]:
        try:
            normalized = validate_agent_name(name)
            registry = load_agent_registry()
            entry = registry.agents.get(normalized)
            if entry is None:
                raise AgentRegistryError(f"Unknown agent {normalized!r}.")
            return registry, entry
        except Exception as exc:
            raise _registry_error(exc) from exc

    def list_agents(self, channel_service: "ChannelService") -> dict[str, Any]:
        registry = self.registry()
        return {
            "active_agent": registry.active_agent,
            "agents": [
                self.summary_for_entry(registry, entry, channel_service=channel_service)
                for entry in sorted(registry.agents.values(), key=lambda item: item.name)
            ],
        }

    def summary_for_entry(
        self,
        registry: AgentRegistry,
        entry: AgentEntry,
        *,
        channel_service: "ChannelService",
    ) -> dict[str, Any]:
        overview = build_runtime_overview(entry.path)
        config = load_config_file(entry.path)
        provider_cfg = config.get("provider") if isinstance(config.get("provider"), dict) else {}
        provider = str(provider_cfg.get("name") or "").strip() if isinstance(provider_cfg, dict) else ""
        model = str(provider_cfg.get("model") or "").strip() if isinstance(provider_cfg, dict) else ""
        issues = [
            f"{item.name}: {item.detail or item.value}"
            for item in overview.items
            if item.status == "error"
        ]
        return {
            "name": entry.name,
            "title": entry.title,
            "path": str(entry.path),
            "active": entry.name == registry.active_agent,
            "initialized": overview.initialized,
            "headline": overview.headline,
            "provider": provider,
            "model": model,
            "issues": issues,
            "channels": channel_service.channel_states(entry.name, config=config, config_dir=entry.path),
        }

    def create_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = validate_agent_name(str(payload.get("name") or ""))
        title = str(payload.get("title") or "").strip() or None
        make_active = bool(payload.get("make_active", False))
        registry = load_agent_registry_or_empty()
        if name in registry.agents:
            raise HTTPException(status_code=409, detail=f"Agent {name!r} already exists")
        path = default_agent_dir(name)
        if path.exists() and any(path.iterdir()):
            raise HTTPException(status_code=409, detail=f"Agent directory already has contents: {path}")

        selection = init_selection_from_payload(payload)
        result = init_agent_directory(str(path), force=False, selection=selection, clear_runtime_data=False)
        if not result.wrote_files:
            raise HTTPException(status_code=409, detail="Agent files already exist")
        updated = register_agent(name, path=path, title=title, make_active=make_active or not registry.agents)
        entry = updated.agents[name]

        feishu = payload.get("feishu") if isinstance(payload.get("feishu"), dict) else {}
        if feishu.get("enabled") and feishu.get("mode") == "manual":
            AgentConfigService().write_feishu_manual(
                entry,
                app_id=str(feishu.get("app_id") or "").strip(),
                app_secret=str(feishu.get("app_secret") or "").strip(),
                stream=bool(feishu.get("stream", False)),
                group_fetch_limit=int(feishu.get("group_fetch_limit") or 10),
                group_reply_only_when_mentioned=bool(feishu.get("group_reply_only_when_mentioned", False)),
            )

        return {"status": "ok", "agent": {"name": entry.name, "title": entry.title, "path": str(entry.path)}}

    def update_agent(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        registry, entry = self.require_entry(name)
        title = str(payload.get("title") or entry.title).strip() or entry.title
        agents = dict(registry.agents)
        agents[entry.name] = AgentEntry(name=entry.name, title=title, path=entry.path)
        updated = AgentRegistry(active_agent=registry.active_agent, agents=agents)
        from ..cli.agents import save_agent_registry

        save_agent_registry(updated)
        return {"status": "ok", "agent": {"name": entry.name, "title": title, "path": str(entry.path)}}

    def select_agent(self, name: str) -> dict[str, Any]:
        try:
            registry = select_agent(name)
            entry = registry.agents[registry.active_agent]
            return {"status": "ok", "active_agent": entry.name}
        except Exception as exc:
            raise _registry_error(exc) from exc

    def delete_agent(self, name: str, *, stop_running_channels: bool = False) -> dict[str, Any]:
        _registry, entry = self.require_entry(name)
        channel_service = ChannelService(self)
        running = [
            state["channel"]
            for state in channel_service.channel_states(name, config_dir=entry.path)
            if state["status"] == "running"
        ]
        if running and not stop_running_channels:
            raise HTTPException(
                status_code=409,
                detail=f"Stop running channels before deleting: {', '.join(running)}",
            )
        if running:
            for channel in running:
                channel_service.stop_channel(name, channel)
        try:
            deleted = delete_agent_directory(entry.path)
            _updated, removed = remove_agent(name)
        except Exception as exc:
            raise _registry_error(exc) from exc
        return {"status": "ok", "deleted": {"name": removed.name, "path": str(removed.path), "directory_deleted": deleted}}


def init_selection_from_payload(payload: dict[str, Any]) -> InitSelection:
    model = payload.get("model") if isinstance(payload.get("model"), dict) else {}
    capabilities = payload.get("capabilities") if isinstance(payload.get("capabilities"), dict) else {}
    voice = payload.get("voice") if isinstance(payload.get("voice"), dict) else {}
    identity = str(payload.get("identity") or "").strip()
    if not identity:
        identity = "# Identity\n\nDescribe this agent's role, tone, and behavior here.\n"
    else:
        identity = _format_identity_markdown(identity)

    voice_provider = str(voice.get("provider") or "none").strip().lower()
    voice_enabled = bool(voice.get("enabled")) and voice_provider != "none"

    return InitSelection(
        provider=str(model.get("provider") or "openai").strip().lower(),
        base_url=str(model.get("base_url") or "https://api.openai.com/v1").strip(),
        api_key=str(model.get("api_key") or API_KEY_PLACEHOLDER).strip() or API_KEY_PLACEHOLDER,
        model=str(model.get("model") or "gpt-5.4-mini").strip(),
        model_api=str(model.get("model_api") or "").strip(),
        supports_vision=bool(model.get("supports_vision", False)),
        identity=identity,
        search_provider=str(capabilities.get("search_provider") or "none").strip().lower(),
        search_api_key=str(capabilities.get("search_api_key") or "").strip(),
        image_generation_provider=str(capabilities.get("image_generation_provider") or "none").strip().lower(),
        image_generation_api_key=str(capabilities.get("image_generation_api_key") or "").strip(),
        observability_enabled=bool(capabilities.get("observability_enabled", False)),
        langfuse_public_key=str(capabilities.get("langfuse_public_key") or "").strip(),
        langfuse_secret_key=str(capabilities.get("langfuse_secret_key") or "").strip(),
        langfuse_base_url=str(capabilities.get("langfuse_base_url") or "").strip(),
        voice_enabled=voice_enabled,
        voice_provider=voice_provider,
        voice_api_key=str(voice.get("api_key") or "").strip(),
        voice_stt_provider=str(voice.get("stt_provider") or "").strip(),
        voice_stt_api_key=str(voice.get("stt_api_key") or "").strip(),
        voice_tts_provider=str(voice.get("tts_provider") or "").strip(),
        voice_tts_api_key=str(voice.get("tts_api_key") or "").strip(),
        voice_enable_interruptions=bool(voice.get("enable_interruptions", False)),
        voice_wake_enabled=bool(voice.get("wake_enabled", False)),
        voice_wake_phrases=tuple(str(item).strip() for item in voice.get("wake_phrases") or () if str(item).strip()),
        voice_exit_phrases=tuple(str(item).strip() for item in voice.get("exit_phrases") or () if str(item).strip()),
    )


class AgentConfigService:
    def __init__(self, registry_service: AgentRegistryService | None = None):
        self.registry_service = registry_service or AgentRegistryService()

    def overview(self, name: str) -> dict[str, Any]:
        registry, entry = self.registry_service.require_entry(name)
        overview = build_runtime_overview(entry.path)
        config = load_config_file(entry.path)
        return {
            "agent": self.registry_service.summary_for_entry(registry, entry, channel_service=ChannelService(self.registry_service)),
            "overview": {
                "config_dir": str(overview.config_dir),
                "initialized": overview.initialized,
                "headline": overview.headline,
                "items": _overview_items(overview),
            },
            "missing_secrets": _missing_secret_paths(config),
        }

    def read_identity(self, name: str) -> dict[str, Any]:
        _registry, entry = self.registry_service.require_entry(name)
        path = _agent_identity_path(entry)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="identity.md not found")
        return {
            "identity": path.read_text(encoding="utf-8"),
            "path": str(path),
            "filename": path.name,
            "modified": path.stat().st_mtime,
        }

    def write_identity(self, name: str, identity: str) -> dict[str, Any]:
        _registry, entry = self.registry_service.require_entry(name)
        content = identity.strip()
        if not content:
            raise HTTPException(status_code=400, detail="Identity cannot be empty")
        path = _agent_identity_path(entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{content}\n", encoding="utf-8")
        return self.read_identity(name)

    def read_config(self, name: str) -> dict[str, Any]:
        _registry, entry = self.registry_service.require_entry(name)
        config = load_config_file(entry.path)
        return {
            "path": str(_agent_config_path(entry)),
            "config": _redact_config(config),
            "missing_secrets": _missing_secret_paths(config),
        }

    def preview_config(self, name: str, candidate: dict[str, Any]) -> dict[str, Any]:
        _registry, entry = self.registry_service.require_entry(name)
        current = load_config_file(entry.path)
        restored = _restore_masked_secrets(candidate, current)
        try:
            validate_config(restored)
        except Exception as exc:
            return {
                "valid": False,
                "changes": [],
                "errors": [str(exc)],
                "restart_required_channels": [],
            }
        changes = config_changes(current, restored)
        return {
            "valid": True,
            "changes": changes,
            "errors": [],
            "restart_required_channels": restart_required_channels(current, restored, entry.path),
        }

    def write_config(self, name: str, candidate: dict[str, Any]) -> dict[str, Any]:
        _registry, entry = self.registry_service.require_entry(name)
        current = load_config_file(entry.path)
        restored = _restore_masked_secrets(candidate, current)
        preview = self.preview_config(name, restored)
        if not preview["valid"]:
            raise HTTPException(status_code=400, detail="; ".join(preview["errors"]))
        write_config(entry.path, restored)
        return {"status": "ok", **self.read_config(name), "preview": preview}

    def write_feishu_manual(
        self,
        entry: AgentEntry,
        *,
        app_id: str,
        app_secret: str,
        stream: bool = False,
        group_fetch_limit: int = 10,
        group_reply_only_when_mentioned: bool = False,
    ) -> dict[str, Any]:
        if not app_id or not app_secret:
            raise HTTPException(status_code=400, detail="Feishu App ID and App Secret are required")
        config_path = _agent_config_path(entry)
        config = load_config_file(entry.path)
        channels_cfg = config.setdefault("channels", {})
        if not isinstance(channels_cfg, dict):
            raise HTTPException(status_code=400, detail="channels must be a dictionary")
        _ensure_api_port(channels_cfg)
        channels_cfg["feishu"] = _feishu_channel_config(
            FeishuInitSelection(
                app_id=app_id,
                app_secret=app_secret,
                stream=stream,
                group_fetch_limit=group_fetch_limit,
                group_reply_only_when_mentioned=group_reply_only_when_mentioned,
                credential_mode="manual",
            )
        )
        validate_config(config)
        config_path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=False), encoding="utf-8")
        return {"status": "ok", "channel": "feishu"}

    def write_weixin_config(self, entry: AgentEntry, selection: WeixinInitSelection) -> dict[str, Any]:
        config_path = _agent_config_path(entry)
        config = load_config_file(entry.path)
        channels_cfg = config.setdefault("channels", {})
        if not isinstance(channels_cfg, dict):
            raise HTTPException(status_code=400, detail="channels must be a dictionary")
        _ensure_api_port(channels_cfg)
        channels_cfg["weixin"] = _weixin_channel_config(selection)
        validate_config(config)
        config_path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=False), encoding="utf-8")
        return {"status": "ok", "channel": "weixin"}


def config_changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    keys = sorted(set(flatten_config(before)) | set(flatten_config(after)))
    before_flat = flatten_config(before)
    after_flat = flatten_config(after)
    for key in keys:
        if before_flat.get(key) == after_flat.get(key):
            continue
        changes.append({
            "path": key,
            "before": SECRET_MASK if key.rsplit(".", 1)[-1] in _SECRET_KEYS else str(before_flat.get(key, "(unset)")),
            "after": SECRET_MASK if key.rsplit(".", 1)[-1] in _SECRET_KEYS else str(after_flat.get(key, "(unset)")),
        })
    return changes


def flatten_config(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            result.update(flatten_config(item, path))
        return result
    if isinstance(value, list):
        return {prefix: json.dumps(value, sort_keys=True)}
    return {prefix: value}


def restart_required_channels(before: dict[str, Any], after: dict[str, Any], config_dir: Path) -> list[str]:
    changed_paths = {item["path"] for item in config_changes(before, after)}
    channels: set[str] = set()
    if any(path.startswith("provider.") or path.startswith("agent.") or path.startswith("search.") or path.startswith("image_generation.") for path in changed_paths):
        channels.update(VALID_CHANNELS)
    for channel in VALID_CHANNELS:
        if any(path.startswith(f"channels.{channel}.") for path in changed_paths):
            channels.add(channel)
    return sorted(
        channel
        for channel in channels
        if running_pid(managed_paths(config_dir, channel).pid_path) is not None
    )


class ChannelService:
    def __init__(self, registry_service: AgentRegistryService | None = None):
        self.registry_service = registry_service or AgentRegistryService()

    def channel_states(
        self,
        name: str,
        *,
        config: dict[str, Any] | None = None,
        config_dir: Path | None = None,
    ) -> list[dict[str, Any]]:
        if config_dir is None:
            _registry, entry = self.registry_service.require_entry(name)
            config_dir = entry.path
        if config is None:
            config = load_config_file(config_dir)
        enabled = set(enabled_channels_from_config(config))
        return [self.channel_state(config_dir, config, channel, enabled=enabled) for channel in (CHANNEL_API, CHANNEL_VOICE, CHANNEL_FEISHU, CHANNEL_WEIXIN)]

    def channel_state(
        self,
        config_dir: Path,
        config: dict[str, Any],
        channel: str,
        *,
        enabled: set[str] | None = None,
    ) -> dict[str, Any]:
        if channel not in VALID_CHANNELS:
            raise HTTPException(status_code=404, detail=f"Unknown channel: {channel}")
        enabled = enabled or set(enabled_channels_from_config(config))
        paths = managed_paths(config_dir, channel)
        pid = running_pid(paths.pid_path)
        configured = self._configured(config, channel)
        target = self._target(config, channel)
        recent_log = tail_text(paths.log_path, max_lines=40)
        return {
            "channel": channel,
            "configured": configured,
            "enabled": channel in enabled,
            "status": "running" if pid is not None else ("disabled" if channel not in enabled else "stopped"),
            "pid": pid,
            "target": target,
            "pid_path": str(paths.pid_path),
            "log_path": str(paths.log_path),
            "recent_log": recent_log,
            "restart_required": False,
        }

    @staticmethod
    def _configured(config: dict[str, Any], channel: str) -> bool:
        if channel == CHANNEL_API:
            return api_config(config).get("enabled", True) is not False
        if channel == CHANNEL_FEISHU:
            data = feishu_config(config)
            return bool(data.get("app_id") and data.get("app_secret"))
        if channel == CHANNEL_WEIXIN:
            return bool(weixin_config(config).get("account_id"))
        if channel == CHANNEL_VOICE:
            data = voice_config(config)
            return bool(data) and data.get("enabled", True) is not False
        return False

    @staticmethod
    def _target(config: dict[str, Any], channel: str) -> str:
        if channel == CHANNEL_API:
            return _api_service_url(config)
        if channel == CHANNEL_VOICE:
            data = voice_config(config)
            provider = data.get("provider") if isinstance(data, dict) else ""
            return str(provider or "local")
        if channel == CHANNEL_FEISHU:
            return str(feishu_config(config).get("app_id") or "")
        if channel == CHANNEL_WEIXIN:
            return str(weixin_config(config).get("account_id") or "")
        return ""

    def start_channel(self, name: str, channel: str) -> dict[str, Any]:
        _registry, entry = self.registry_service.require_entry(name)
        if channel not in VALID_CHANNELS:
            raise HTTPException(status_code=404, detail=f"Unknown channel: {channel}")
        if channel == CHANNEL_API:
            self._ensure_api_port_not_reserved(entry.path)
        result = cli_runtime.handle_start(_channel_args(entry.path, channel))
        if result != 0:
            state = self.channel_state(entry.path, load_config_file(entry.path), channel)
            raise HTTPException(status_code=500, detail=state.get("recent_log") or f"Failed to start {channel}")
        return {"status": "ok", "channel": self.channel_state(entry.path, load_config_file(entry.path), channel)}

    @staticmethod
    def _ensure_api_port_not_reserved(config_dir: Path) -> bool:
        config = load_config_file(config_dir)
        channels_cfg = config.get("channels")
        if not isinstance(channels_cfg, dict):
            return False
        api_cfg = channels_cfg.get(CHANNEL_API)
        if not isinstance(api_cfg, dict):
            return False
        try:
            port = int(api_cfg.get("port") or CONSOLE_PORT)
        except (TypeError, ValueError):
            port = CONSOLE_PORT
        if port != CONSOLE_PORT:
            return False
        api_cfg["port"] = allocate_api_port()
        write_config(config_dir, config)
        return True

    def stop_channel(self, name: str, channel: str) -> dict[str, Any]:
        _registry, entry = self.registry_service.require_entry(name)
        if channel not in VALID_CHANNELS:
            raise HTTPException(status_code=404, detail=f"Unknown channel: {channel}")
        stopped, message = stop_managed_process(managed_paths(entry.path, channel).pid_path)
        if not stopped:
            raise HTTPException(status_code=500, detail=message)
        return {
            "status": "ok",
            "message": message,
            "channel": self.channel_state(entry.path, load_config_file(entry.path), channel),
        }

    def restart_channel(self, name: str, channel: str) -> dict[str, Any]:
        self.stop_channel(name, channel)
        return self.start_channel(name, channel)

    def logs(self, name: str, channel: str, *, lines: int = 120) -> dict[str, Any]:
        _registry, entry = self.registry_service.require_entry(name)
        if channel not in VALID_CHANNELS:
            raise HTTPException(status_code=404, detail=f"Unknown channel: {channel}")
        paths = managed_paths(entry.path, channel)
        return {
            "channel": channel,
            "log_path": str(paths.log_path),
            "content": tail_text(paths.log_path, max_lines=max(1, min(int(lines), 1000))),
        }

    def api_is_running(self, name: str) -> tuple[bool, str, str]:
        _registry, entry = self.registry_service.require_entry(name)
        config = load_config_file(entry.path)
        state = self.channel_state(entry.path, config, CHANNEL_API)
        return state["status"] == "running", _api_service_url(config), _api_ws_url(config)


class AgentDataService:
    def __init__(self, registry_service: AgentRegistryService | None = None):
        self.registry_service = registry_service or AgentRegistryService()

    def _entry(self, name: str) -> AgentEntry:
        _registry, entry = self.registry_service.require_entry(name)
        return entry

    def memory_root(self, name: str) -> Path:
        return self._entry(name).path / BaseAgentConfig.MEMORY_DIRNAME

    def workspace_root(self, name: str) -> Path:
        return self._entry(name).path / BaseAgentConfig.WORKSPACE_DIRNAME

    def skills_root(self, name: str) -> Path:
        return self._entry(name).path / BaseAgentConfig.SKILLS_DIRNAME

    def tasks_root(self, name: str) -> Path:
        return self._entry(name).path / BaseAgentConfig.TASKS_DIRNAME

    def message_storage(self, name: str) -> MessageStorage:
        path = self._entry(name).path / BaseAgentConfig.MESSAGE_DIRNAME / BaseAgentConfig.MESSAGE_DB_FILENAME
        return MessageStorage(path=str(path))

    def memory_tree(self, name: str) -> dict[str, Any]:
        root = self.memory_root(name)
        if not root.is_dir():
            return {"tree": []}
        return {"tree": self._scan_markdown_tree(root, root)}

    def memory_read(self, name: str, path: str) -> dict[str, Any]:
        root = self.memory_root(name)
        requested = (root / path).resolve()
        if not requested.is_relative_to(root):
            raise HTTPException(status_code=403, detail="Access denied")
        if not requested.is_file() or requested.suffix != ".md":
            raise HTTPException(status_code=404, detail="Memory file not found")
        return {"path": path, "content": requested.read_text(encoding="utf-8"), "modified": requested.stat().st_mtime}

    def memory_search(self, name: str, query: str, limit: int = 50) -> dict[str, Any]:
        root = self.memory_root(name)
        needle = query.strip().lower()
        results: list[dict[str, Any]] = []
        if root.is_dir():
            for path in sorted(root.rglob("*.md")):
                if len(results) >= limit:
                    break
                relative = str(path.relative_to(root))
                text = path.read_text(encoding="utf-8", errors="replace")
                idx = text.lower().find(needle)
                matched = []
                snippet = ""
                if needle in relative.lower():
                    matched.append("filename")
                if idx != -1:
                    matched.append("content")
                    snippet = text[max(0, idx - 80): idx + len(query) + 120].replace("\n", " ").strip()
                if matched:
                    results.append({"name": path.name, "path": relative, "matched_in": matched, "snippet": snippet, "modified": path.stat().st_mtime})
        return {"query": query, "results": results}

    def memory_clear(self, name: str) -> dict[str, Any]:
        import shutil

        root = self.memory_root(name)
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True, exist_ok=True)
        return {"status": "ok"}

    def workspace_files(self, name: str) -> WorkspaceFileService:
        return WorkspaceFileService(self.workspace_root(name))

    async def workspace_upload(self, name: str, file: UploadFile, path: str = "") -> dict[str, Any]:
        workspace_files = self.workspace_files(name)
        raw_target = path.strip()
        filename = safe_attachment_filename(file.filename or "upload.bin")
        content = await file.read()
        content_type = (file.content_type or "").split(";", 1)[0].strip().lower()
        detected_mime_type = detect_image_mime(content)
        guessed_mime_type, _ = mimetypes.guess_type(filename)
        looks_like_image = bool(
            detected_mime_type
            or content_type.startswith("image/")
            or (guessed_mime_type and guessed_mime_type.startswith("image/"))
        )
        if looks_like_image:
            if not detected_mime_type:
                raise HTTPException(status_code=415, detail="Uploaded image data is not a supported PNG, JPEG, or WebP file")
            if len(content) > MAX_IMAGE_BYTES:
                raise HTTPException(status_code=413, detail="Image upload exceeds 10MB")
            if detected_mime_type not in SUPPORTED_UPLOAD_IMAGE_MIME_TYPES:
                allowed = ", ".join(sorted(SUPPORTED_UPLOAD_IMAGE_MIME_TYPES))
                raise HTTPException(status_code=415, detail=f"Unsupported image MIME type; allowed: {allowed}")
        elif len(content) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(status_code=413, detail="File upload exceeds 50MB")
        if raw_target:
            requested = workspace_files.resolve_upload_path(raw_target, filename)
        else:
            directory = DEFAULT_WEB_IMAGE_DIR if looks_like_image else DEFAULT_WEB_ATTACHMENT_DIR
            requested = workspace_files.resolve_upload_path(
                f"{directory}/",
                _content_addressed_upload_name(filename, content),
            )
        requested.parent.mkdir(parents=True, exist_ok=True)
        requested.write_bytes(content)
        metadata = workspace_files.metadata(requested)
        return {
            "status": "ok",
            **metadata,
            "blob_url": f"/api/console/agents/{name}/workspace/blob?path={metadata['path']}",
        }

    def skills_storage(self, name: str) -> SkillsStorageLocal:
        return SkillsStorageLocal(self.skills_root(name))

    async def messages(self, name: str, *, count: int, offset: int) -> dict[str, Any]:
        storage = self.message_storage(name)
        total = await storage.get_message_count()
        messages = await storage.get_messages(count=count, offset=offset)
        items = [message_item(msg) for msg in messages]
        items.reverse()
        return {"messages": items, "total": total, "count": count, "offset": offset, "has_more": offset + count < total}

    async def message_search(self, name: str, query: str, limit: int = 50) -> dict[str, Any]:
        storage = self.message_storage(name)
        total = await storage.get_message_count()
        messages = await storage.get_messages(count=total, offset=0) if total else []
        results: list[dict[str, Any]] = []
        for message in reversed(messages):
            match = message_search_result(message, query)
            if match is None:
                continue
            results.append(match)
            if len(results) >= limit:
                break
        return {"query": query, "results": results}

    async def message_stats(self, name: str) -> dict[str, Any]:
        storage = self.message_storage(name)
        total = await storage.get_message_count()
        info = storage.get_stream_info() if hasattr(storage, "get_stream_info") else {}
        return {"total": total, "storage": info}

    async def clear_messages(self, name: str) -> dict[str, Any]:
        await self.message_storage(name).clear_messages()
        return {"status": "ok", "message": "Message stream cleared"}

    def tasks(self, name: str) -> dict[str, Any]:
        root = self.tasks_root(name)
        tasks = [record.to_task_view() for record in list_task_records(root)]
        return {"root": str(root), "tasks": tasks, "total": len(tasks)}

    def delete_task(self, name: str, task_id: str) -> dict[str, Any]:
        try:
            task = delete_scheduled_task(self.tasks_root(name), task_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "ok", "deleted": task.to_task_view()}

    def _scan_markdown_tree(self, directory: Path, root: Path) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        try:
            children = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            return entries
        for child in children:
            rel = child.relative_to(root)
            if child.is_dir():
                entries.append({"name": child.name, "path": str(rel), "type": "dir", "children": self._scan_markdown_tree(child, root)})
            elif child.suffix == ".md":
                entries.append({"name": child.name, "path": str(rel), "type": "file", "modified": child.stat().st_mtime})
        return entries


@dataclass
class SetupSession:
    session_id: str
    agent_name: str
    kind: str
    created_at: float = field(default_factory=time.time)
    cancelled: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)

    def emit(self, **event: Any) -> None:
        self.events.append({"timestamp": time.time(), **event})


class SetupSessionService:
    """Best-effort async setup bridge for QR / one-click channel onboarding."""

    def __init__(
        self,
        registry_service: AgentRegistryService | None = None,
        config_service: AgentConfigService | None = None,
    ):
        self.registry_service = registry_service or AgentRegistryService()
        self.config_service = config_service or AgentConfigService(self.registry_service)
        self._sessions: dict[str, SetupSession] = {}

    def create_session(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        _registry, entry = self.registry_service.require_entry(name)
        kind = str(payload.get("kind") or payload.get("channel") or "").strip().lower()
        if kind not in {"feishu", "weixin"}:
            raise HTTPException(status_code=400, detail="kind must be feishu or weixin")
        session = SetupSession(session_id=uuid.uuid4().hex, agent_name=name, kind=kind)
        self._sessions[session.session_id] = session
        session.emit(phase="queued", message=f"{kind} setup queued")
        thread = threading.Thread(target=self._run_session, args=(session, entry, payload), daemon=True)
        thread.start()
        return {"session_id": session.session_id, "kind": kind, "events": session.events}

    def get_session(self, session_id: str) -> SetupSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Setup session not found")
        return session

    def cancel_session(self, session_id: str) -> dict[str, Any]:
        session = self.get_session(session_id)
        session.cancelled = True
        session.emit(phase="cancelled", message="Setup session cancelled")
        return {"status": "ok"}

    def _run_session(self, session: SetupSession, entry: AgentEntry, payload: dict[str, Any]) -> None:
        try:
            if session.kind == "feishu":
                self._run_feishu_session(session, entry, payload)
            else:
                self._run_weixin_session(session, entry, payload)
        except Exception as exc:
            session.emit(phase="error", error=str(exc), message=str(exc))

    def _run_feishu_session(self, session: SetupSession, entry: AgentEntry, payload: dict[str, Any]) -> None:
        mode = str(payload.get("mode") or "manual").strip().lower()
        if mode == "manual":
            session.emit(phase="configuring", message="Writing Feishu manual credentials")
            self.config_service.write_feishu_manual(
                entry,
                app_id=str(payload.get("app_id") or "").strip(),
                app_secret=str(payload.get("app_secret") or "").strip(),
                stream=bool(payload.get("stream", False)),
                group_fetch_limit=int(payload.get("group_fetch_limit") or 10),
                group_reply_only_when_mentioned=bool(payload.get("group_reply_only_when_mentioned", False)),
            )
            session.emit(phase="done", result={"channel": "feishu"}, message="Feishu channel configured")
            return

        session.emit(phase="starting", message="Starting Feishu one-click registration")
        from ..cli.setup import _register_feishu_app_via_qr

        credentials = _register_feishu_app_via_qr()
        if credentials is None:
            raise RuntimeError("Feishu one-click registration did not complete")
        app_id, app_secret = credentials
        self.config_service.write_feishu_manual(
            entry,
            app_id=app_id,
            app_secret=app_secret,
            stream=bool(payload.get("stream", False)),
            group_fetch_limit=int(payload.get("group_fetch_limit") or 10),
            group_reply_only_when_mentioned=bool(payload.get("group_reply_only_when_mentioned", False)),
        )
        session.emit(phase="done", result={"channel": "feishu", "app_id": app_id}, message="Feishu channel configured")

    def _run_weixin_session(self, session: SetupSession, entry: AgentEntry, payload: dict[str, Any]) -> None:
        session.emit(phase="starting", message="Starting Weixin QR login")
        from ...integrations.weixin.client import qr_login
        from ...integrations.weixin.config import ILINK_BASE_URL, WEIXIN_CDN_BASE_URL
        from ...integrations.weixin.state import WeixinStateStore

        base_url = str(payload.get("base_url") or ILINK_BASE_URL).strip().rstrip("/")
        cdn_base_url = str(payload.get("cdn_base_url") or WEIXIN_CDN_BASE_URL).strip().rstrip("/")
        bot_type = str(payload.get("bot_type") or "3").strip() or "3"

        def log(message: str) -> None:
            session.emit(phase="log", message=message)

        def render_qr(url: str) -> None:
            session.emit(phase="qr", qr_url=url, message="Scan this QR code with WeChat")

        credentials = asyncio.run(qr_login(base_url=base_url, bot_type=bot_type, log=log, render_qr_url=render_qr))
        WeixinStateStore(entry.path).save_credentials(credentials)
        selection = WeixinInitSelection(
            account_id=credentials.account_id,
            owner_user_id=credentials.user_id,
            base_url=credentials.base_url or base_url,
            cdn_base_url=cdn_base_url,
            owner_only=bool(payload.get("owner_only", True)),
            allow_users=tuple(str(item).strip() for item in payload.get("allow_users") or () if str(item).strip()),
            media_enabled=bool(payload.get("media_enabled", True)),
        )
        self.config_service.write_weixin_config(entry, selection)
        session.emit(phase="done", result={"channel": "weixin", "account_id": selection.account_id}, message="Weixin channel configured")
