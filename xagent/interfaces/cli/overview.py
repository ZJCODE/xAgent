"""Launcher runtime overview aggregation."""
from __future__ import annotations

import sqlite3
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...core.providers import provider_model_api
from ...core.runtime import list_task_records
from ...core.runtime.ownership import runtime_is_active, runtime_paths
from ...tools.image_generation_tool import normalize_image_generation_provider
from ...tools.search_tool import is_placeholder_api_key, normalize_search_provider
from ..base import BaseAgentConfig, BaseAgentRunner
from ..voice.config import VoiceChannelConfig
from .channels import (
    CHANNEL_API,
    CHANNEL_FEISHU,
    CHANNEL_VOICE,
    CHANNEL_WEIXIN,
    api_config,
    feishu_config,
    load_config_file,
    voice_config,
    weixin_config,
)
from .processes import running_pid
from .web_client import DEFAULT_WEB_CLIENT_PORT, web_client_config, web_client_paths

STATUS_OK = "ok"
STATUS_IDLE = "idle"
STATUS_WARNING = "warning"
STATUS_ERROR = "error"
STATUS_DISABLED = "disabled"


def _api_key_detail() -> str:
    return "API key"


def _friendly_overview_error(message: str, *, fallback: str = "Check settings") -> str:
    normalized = " ".join(str(message).split()).lower()
    if "api key" in normalized or "api_key" in normalized:
        return _api_key_detail()
    if "provider is required" in normalized:
        return "Provider"
    return fallback


def _api_service_target(config: dict[str, Any]) -> str:
    return _api_service_url(config).removeprefix("http://")


@dataclass(frozen=True)
class OverviewItem:
    """One row in the launcher overview."""

    name: str
    value: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class RuntimeOverview:
    """Compact state used by the launcher home screen."""

    config_dir: Path
    initialized: bool
    headline: str
    items: tuple[OverviewItem, ...]

    @property
    def has_errors(self) -> bool:
        return any(item.status == STATUS_ERROR for item in self.items)

    @property
    def has_warnings(self) -> bool:
        return any(item.status == STATUS_WARNING for item in self.items)


def build_runtime_overview(config_dir: Path) -> RuntimeOverview:
    config_path = config_dir / BaseAgentConfig.CONFIG_FILENAME
    identity_path = config_dir / BaseAgentConfig.IDENTITY_FILENAME
    initialized = config_path.is_file() and _identity_valid(identity_path)
    items: list[OverviewItem] = []

    config: dict[str, Any] = {}
    config_error = ""
    runtime = runtime_snapshot(config_dir)
    if not config_path.is_file():
        items.append(OverviewItem("Config", "missing", STATUS_ERROR, f"Expected {config_path}"))
    else:
        try:
            config = load_config_file(config_dir)
            _validate_config(config)
            items.append(OverviewItem("Config", "valid", STATUS_OK, "identity ready" if initialized else "identity missing"))
        except Exception as exc:
            config_error = str(exc)
            items.append(OverviewItem("Config", "invalid", STATUS_ERROR, config_error))

    if not identity_path.is_file():
        items.append(OverviewItem("Identity", "missing", STATUS_ERROR, f"Expected {identity_path}"))
    elif not _identity_valid(identity_path):
        items.append(OverviewItem("Identity", "empty", STATUS_ERROR, f"Add content to {identity_path}"))

    if config:
        items.extend(
            (
                _model_item(config),
                _search_item(config),
                _image_item(config),
                _runtime_item(runtime),
                _voice_item(config_dir, config, runtime=runtime),
                _service_item(config_dir, CHANNEL_API, api_config(config), runtime=runtime),
                _web_client_item(config_dir, config),
                _service_item(config_dir, CHANNEL_FEISHU, feishu_config(config), runtime=runtime),
                _service_item(config_dir, CHANNEL_WEIXIN, weixin_config(config), runtime=runtime),
                _task_item(config_dir),
            )
        )

    if not initialized:
        headline = "Setup required"
    elif config_error or any(item.status == STATUS_ERROR for item in items):
        headline = "Needs attention"
    elif any(item.status == STATUS_WARNING for item in items):
        headline = "Ready with warnings"
    else:
        headline = "Ready"
    return RuntimeOverview(config_dir=config_dir, initialized=initialized, headline=headline, items=tuple(items))


def runtime_snapshot(config_dir: Path) -> dict[str, Any]:
    """Use the runtime's published state only while its authoritative lock is held."""
    if not runtime_is_active(config_dir):
        return {"state": "stopped", "runtime_ready": False, "channels": {}}
    try:
        raw = json.loads(runtime_paths(config_dir).state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict) or raw.get("state") in {None, "stopped"}:
        return {"state": "starting", "runtime_ready": False, "channels": {}}
    if not isinstance(raw.get("channels"), dict):
        raw["channels"] = {}
    return raw


def _runtime_item(runtime: dict[str, Any]) -> OverviewItem:
    state = str(runtime.get("state") or "stopped")
    ready = bool(runtime.get("runtime_ready"))
    if state == "degraded":
        return OverviewItem("Runtime", "degraded", STATUS_WARNING, "Some channels need attention")
    if state == "running" and ready:
        return OverviewItem("Runtime", "ready", STATUS_OK, f"pid {runtime.get('pid', '')}".strip())
    return OverviewItem("Runtime", state, STATUS_IDLE)


def _channel_state(runtime: dict[str, Any], channel: str) -> tuple[str, str, str]:
    if runtime.get("state") == "stopped":
        return "stopped", STATUS_IDLE, ""
    channel_row = runtime.get("channels", {}).get(channel)
    if not isinstance(channel_row, dict):
        return "inactive", STATUS_IDLE, "Restart the Agent to include this channel"
    state = str(channel_row.get("state") or channel_row.get("status") or "starting")
    return state, STATUS_WARNING if state == "failed" else STATUS_OK if state == "running" else STATUS_IDLE, str(channel_row.get("error") or "")


def _task_item(config_dir: Path) -> OverviewItem:
    root = config_dir / BaseAgentConfig.TASKS_DIRNAME
    try:
        records = list_task_records(root, include_running=True) if root.exists() else []
    except (OSError, ValueError):
        return OverviewItem("Tasks", "unavailable", STATUS_WARNING, "Check task storage")
    review = sum(record.status == "needs_review" for record in records)
    failed = sum(record.status == "failed" for record in records)
    if review:
        return OverviewItem("Tasks", f"{review} need review", STATUS_WARNING,
                            "Check uncertain outcomes before retrying")
    if failed:
        return OverviewItem("Tasks", f"{failed} failed", STATUS_WARNING)
    active = sum(record.status == "active" for record in records)
    return OverviewItem("Tasks", f"{active} active", STATUS_OK)


def _identity_valid(path: Path) -> bool:
    try:
        return path.is_file() and bool(path.read_text(encoding="utf-8").strip())
    except OSError:
        return False


def _validate_config(config: dict[str, Any]) -> None:
    runner = BaseAgentRunner.__new__(BaseAgentRunner)
    runner._validate_config(config)


def _provider_cfg(config: dict[str, Any]) -> dict[str, Any]:
    provider = config.get("provider")
    return dict(provider) if isinstance(provider, dict) else {}


def _model_item(config: dict[str, Any]) -> OverviewItem:
    provider = _provider_cfg(config)
    provider_name = str(provider.get("name") or "openai").strip()
    model = str(provider.get("model") or "").strip() or "(missing model)"
    api_key = str(provider.get("api_key") or "").strip()
    status = STATUS_ERROR if is_placeholder_api_key(api_key) else STATUS_OK
    detail = f"API {provider_model_api(provider)}"
    if status == STATUS_ERROR:
        detail = _api_key_detail()
    return OverviewItem("Model", f"{provider_name} / {model}", status, detail)


def _feature_needs_key(config: dict[str, Any], provider: str) -> bool:
    model_provider = str(_provider_cfg(config).get("name") or "").strip().lower()
    return provider not in {"", "none"} and provider != model_provider


def _search_item(config: dict[str, Any]) -> OverviewItem:
    search = config.get("search") if isinstance(config.get("search"), dict) else {}
    try:
        provider = normalize_search_provider(search.get("provider") if isinstance(search, dict) else None)
    except ValueError as exc:
        return OverviewItem("Search", "invalid", STATUS_ERROR, str(exc))
    if provider == "none":
        return OverviewItem("Search", "not set", STATUS_DISABLED)
    api_key = str(search.get("api_key") or "").strip() if isinstance(search, dict) else ""
    if _feature_needs_key(config, provider) and is_placeholder_api_key(api_key):
        return OverviewItem("Search", provider, STATUS_ERROR, _api_key_detail())
    detail = "Own key" if api_key and not is_placeholder_api_key(api_key) else "Model key"
    return OverviewItem("Search", provider, STATUS_OK, detail)


def _image_item(config: dict[str, Any]) -> OverviewItem:
    image = config.get("image_generation") if isinstance(config.get("image_generation"), dict) else {}
    try:
        provider = normalize_image_generation_provider(image.get("provider") if isinstance(image, dict) else None)
    except ValueError as exc:
        return OverviewItem("Image", "invalid", STATUS_ERROR, str(exc))
    if provider == "none":
        return OverviewItem("Image", "not set", STATUS_DISABLED)
    api_key = str(image.get("api_key") or "").strip() if isinstance(image, dict) else ""
    if _feature_needs_key(config, provider) and is_placeholder_api_key(api_key):
        return OverviewItem("Image", provider, STATUS_ERROR, _api_key_detail())
    detail = "Own key" if api_key and not is_placeholder_api_key(api_key) else "Model key"
    return OverviewItem("Image", provider, STATUS_OK, detail)


def _voice_item(config_dir: Path, config: dict[str, Any], *, runtime: dict[str, Any] | None = None) -> OverviewItem:
    runtime = runtime or runtime_snapshot(config_dir)
    active = runtime.get("channels", {}).get(CHANNEL_VOICE)
    if isinstance(active, dict):
        state, status, error = _channel_state(runtime, CHANNEL_VOICE)
        return OverviewItem("Voice", state, status, error or "soniox voice")
    raw_voice = voice_config(config)
    if not raw_voice:
        return OverviewItem("Voice", "not set", STATUS_DISABLED)
    try:
        voice = VoiceChannelConfig.from_dict(raw_voice)
    except Exception as exc:
        return OverviewItem("Voice", "invalid", STATUS_ERROR, _friendly_overview_error(str(exc)))
    try:
        voice.resolved_api_key()
    except ValueError as exc:
        return OverviewItem("Voice", "soniox", STATUS_ERROR, _friendly_overview_error(str(exc), fallback="Setup"))

    state, status, error = _channel_state(runtime or runtime_snapshot(config_dir), CHANNEL_VOICE)
    return OverviewItem("Voice", state, status, error or "soniox voice")


def _api_service_url(config: dict[str, Any]) -> str:
    host = str(config.get("host") or BaseAgentConfig.DEFAULT_HOST).strip() or BaseAgentConfig.DEFAULT_HOST
    port = str(config.get("port") or BaseAgentConfig.DEFAULT_PORT).strip() or str(BaseAgentConfig.DEFAULT_PORT)
    browse_host = host
    if browse_host == "0.0.0.0":
        browse_host = "127.0.0.1"
    if ":" in browse_host and not browse_host.startswith("["):
        browse_host = f"[{browse_host}]"
    return f"http://{browse_host}:{port}"


def _service_item(config_dir: Path, channel: str, config: dict[str, Any], *, runtime: dict[str, Any] | None = None) -> OverviewItem:
    runtime = runtime or runtime_snapshot(config_dir)
    title = {CHANNEL_API: "API", CHANNEL_FEISHU: "Feishu", CHANNEL_WEIXIN: "Weixin"}.get(channel, channel)
    active = runtime.get("channels", {}).get(channel)
    if isinstance(active, dict):
        state, status, error = _channel_state(runtime, channel)
        return OverviewItem(title, state, status, error or (_api_service_target(config) if channel == CHANNEL_API else ""))
    if channel == CHANNEL_FEISHU and not (config.get("app_id") and config.get("app_secret")):
        return OverviewItem("Feishu", "not set", STATUS_DISABLED, "Setup")
    if channel == CHANNEL_WEIXIN and not config.get("account_id"):
        return OverviewItem("Weixin", "not set", STATUS_DISABLED, "Setup")
    if channel == CHANNEL_API and config.get("enabled", True) is False:
        return OverviewItem("API", "off", STATUS_DISABLED, "Resetup")
    state, status, error = _channel_state(runtime or runtime_snapshot(config_dir), channel)
    return OverviewItem(title, state, status, error or (_api_service_target(config) if channel == CHANNEL_API else ""))


def _web_client_item(config_dir: Path, config: dict[str, Any]) -> OverviewItem:
    del config_dir
    web_cfg = web_client_config(config)
    if not web_cfg.get("enabled", True):
        return OverviewItem("Web", "off", STATUS_DISABLED, "web")
    paths = web_client_paths()
    pid = running_pid(paths.pid_path)
    detail = _web_client_target(web_cfg)
    if pid is None:
        return OverviewItem("Web", "stopped", STATUS_IDLE, detail)
    return OverviewItem("Web", "running", STATUS_OK, f"{detail} pid {pid}")


def _web_client_target(web_cfg: dict[str, Any]) -> str:
    host = str(web_cfg.get("host") or BaseAgentConfig.DEFAULT_HOST).strip() or BaseAgentConfig.DEFAULT_HOST
    port = str(web_cfg.get("port") or DEFAULT_WEB_CLIENT_PORT).strip()
    browse_host = host
    if browse_host == "0.0.0.0":
        browse_host = "127.0.0.1"
    if ":" in browse_host and not browse_host.startswith("["):
        browse_host = f"[{browse_host}]"
    return f"{browse_host}:{port}"


def _data_item(config_dir: Path) -> OverviewItem:
    memory_count = _memory_count(config_dir / BaseAgentConfig.MEMORY_DIRNAME)
    message_count = _message_count(config_dir / BaseAgentConfig.MESSAGE_DIRNAME / BaseAgentConfig.MESSAGE_DB_FILENAME)
    active_tasks, failed_tasks = _task_counts(config_dir / BaseAgentConfig.TASKS_DIRNAME)
    status = STATUS_WARNING if failed_tasks else STATUS_OK
    value = f"{memory_count} memory / {message_count} messages"
    detail = "no active tasks" if active_tasks == 0 else _count_phrase(active_tasks, "active task")
    if failed_tasks:
        detail += f", {_count_phrase(failed_tasks, 'failed task')}"
    return OverviewItem("Data", value, status, detail)


def _count_phrase(count: int, singular: str, plural: str | None = None) -> str:
    plural_form = plural or f"{singular}s"
    return f"{count} {singular if count == 1 else plural_form}"


def _memory_count(root: Path) -> int:
    if not root.exists():
        return 0
    return sum(1 for path in root.rglob("*.md") if path.is_file())


def _message_count(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        with sqlite3.connect(str(path)) as connection:
            row = connection.execute("SELECT COUNT(*) FROM messages").fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row else 0


def _task_counts(root: Path) -> tuple[int, int]:
    if not root.exists():
        return 0, 0
    try:
        records = list_task_records(root, include_failed=True)
    except Exception:
        return 0, 0
    active = sum(1 for record in records if record.status == "active")
    failed = sum(1 for record in records if record.state == "failed")
    return active, failed
