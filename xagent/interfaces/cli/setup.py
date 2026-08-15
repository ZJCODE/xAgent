"""Setup and initialization flows for the CLI."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlparse

import yaml
from rich.text import Text  # type: ignore[import-not-found]

from ...core.config import AgentConfig
from ...core.providers import (
    KNOWN_PROVIDERS,
    MODEL_API_ANTHROPIC_MESSAGES,
    MODEL_API_OPENAI_CHAT_COMPLETIONS,
    MODEL_API_OPENAI_RESPONSES,
    PROVIDER_ANTHROPIC,
    PROVIDER_CUSTOM,
    PROVIDER_DEEPSEEK,
    PROVIDER_OPENAI,
    PROVIDER_QWEN,
    ReasoningConfig,
    normalize_provider_name,
    normalize_reasoning_config,
    provider_base_url,
    reasoning_capability,
)
from ..base import BaseAgentConfig
from .agents import allocate_api_port
from .paths import config_path as _config_path
from .paths import runtime_dir as _runtime_dir
from .paths import setup_runtime_dir as _setup_runtime_dir
from .terminal_ui import MenuOption, ReturnToLauncherHome, SetupCancelled, TerminalUI

SETUP_EXIT_CANCELLED = 130


@dataclass(frozen=True)
class InitResult:
    """Result for xagent init file generation."""

    config_path: Path
    identity_path: Path
    memory_dir: Path
    messages_dir: Path
    workspace_dir: Path
    skills_dir: Path
    tasks_dir: Path
    wrote_files: bool
    conflicts: Tuple[Path, ...]


@dataclass(frozen=True)
class InitSelection:
    """Interactive choices used to generate xAgent project files."""

    provider: str
    base_url: str
    api_key: str
    model: str
    identity: str
    model_api: str = ""
    supports_vision: bool = False
    reasoning: Optional[ReasoningConfig] = None


@dataclass(frozen=True)
class FeishuInitSelection:
    """Interactive choices used to configure the Feishu channel."""

    app_id: str
    app_secret: str
    stream: bool = False
    group_fetch_limit: int = 10
    group_reply_only_when_mentioned: bool = False
    credential_mode: str = "one_click"


@dataclass(frozen=True)
class WeixinInitSelection:
    """Interactive choices used to configure the Weixin channel."""

    account_id: str
    owner_user_id: str
    base_url: str
    cdn_base_url: str
    owner_only: bool = True
    allow_users: tuple[str, ...] = ()
    media_enabled: bool = True


@dataclass(frozen=True)
class VoiceInitSelection:
    """Interactive choices used to configure the local voice channel."""

    voice_enabled: bool = True
    voice_api_key: str = ""


OPENAI_BASE_URL = provider_base_url(PROVIDER_OPENAI)
DEEPSEEK_BASE_URL = provider_base_url(PROVIDER_DEEPSEEK)
ANTHROPIC_BASE_URL = provider_base_url(PROVIDER_ANTHROPIC)
QWEN_BASE_URL = provider_base_url(PROVIDER_QWEN)
CUSTOM_OPENAI_BASE_URL_PLACEHOLDER = provider_base_url(PROVIDER_CUSTOM, MODEL_API_OPENAI_CHAT_COMPLETIONS)
CUSTOM_ANTHROPIC_BASE_URL_PLACEHOLDER = provider_base_url(PROVIDER_CUSTOM, MODEL_API_ANTHROPIC_MESSAGES)
API_KEY_PLACEHOLDER = "your_api_key_here"
SONIOX_KEY_PLACEHOLDER = "your_soniox_api_key_here"
MODEL_PLACEHOLDER = "your_model_here"
LANGFUSE_BASE_URL = "https://cloud.langfuse.com"
LANGFUSE_PUBLIC_KEY_PLACEHOLDER = "pk-lf-..."
LANGFUSE_SECRET_KEY_PLACEHOLDER = "sk-lf-..."
CUSTOM_MODEL_OPTION = "Custom"
DEFAULT_MESSAGE_LIST_COUNT = 5
MESSAGE_LIST_COUNT_CHOICES = (2, 5, 10)
DEFAULT_MEMORY_LIST_DAYS = 7
MEMORY_LIST_DAY_CHOICES = (1, 3, 7)

OPENAI_MODELS = (
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.6-sol",
)
ANTHROPIC_MODELS = (
    "claude-sonnet-4-20250514",
    "claude-opus-4-1-20250805",
    "claude-3-5-haiku-20241022",
)
DEEPSEEK_MODELS = (
    "deepseek-v4-flash",
    "deepseek-v4-pro",
)
QWEN_MODELS = (
    "qwen3.7-max",
    "qwen3.6-flash",
    "qwen3.6-plus",
)
SEARCH_PROVIDERS = (
    "none",
    "openai",
    "qwen",
    "minimax",
)
IMAGE_GENERATION_PROVIDERS = (
    "none",
    "openai",
    "minimax",
    "qwen",
)

_PROVIDER_DESCRIPTIONS = {
    PROVIDER_OPENAI: "GPT family via the OpenAI platform.",
    PROVIDER_DEEPSEEK: "DeepSeek chat and coding models.",
    PROVIDER_QWEN: "Qwen models via DashScope-compatible APIs.",
    PROVIDER_ANTHROPIC: "Claude models via Anthropic Messages.",
    PROVIDER_CUSTOM: "Bring your own OpenAI, Responses, or Anthropic endpoint.",
}

_PROVIDER_LABELS = {
    PROVIDER_OPENAI: "OpenAI",
    PROVIDER_DEEPSEEK: "DeepSeek",
    PROVIDER_QWEN: "Qwen",
    PROVIDER_ANTHROPIC: "Anthropic",
    PROVIDER_CUSTOM: "Custom",
}

def init_selection_from_mapping(data: Mapping[str, Any]) -> InitSelection:
    """Build an ``InitSelection`` from API/JSON input."""
    provider = normalize_provider_name(str(data.get("provider") or PROVIDER_OPENAI))
    selected_model = str(data.get("model") or MODEL_PLACEHOLDER).strip() or MODEL_PLACEHOLDER
    model = MODEL_PLACEHOLDER if selected_model == "Decide later" else selected_model

    base_url = str(data.get("base_url") or "").strip()
    if not base_url:
        if provider == PROVIDER_CUSTOM:
            model_api = str(data.get("model_api") or MODEL_API_OPENAI_CHAT_COMPLETIONS)
            base_url = (
                CUSTOM_ANTHROPIC_BASE_URL_PLACEHOLDER
                if model_api == MODEL_API_ANTHROPIC_MESSAGES
                else CUSTOM_OPENAI_BASE_URL_PLACEHOLDER
            )
        else:
            base_url = provider_base_url(provider)

    model_api = str(data.get("model_api") or "")
    reasoning_provider_cfg: dict[str, Any] = {
        "name": provider,
        "model_api": model_api or MODEL_API_OPENAI_CHAT_COMPLETIONS,
        "reasoning": data.get("reasoning"),
    }
    if provider != PROVIDER_CUSTOM:
        reasoning_provider_cfg.pop("model_api", None)
    reasoning = (
        normalize_reasoning_config(reasoning_provider_cfg)
        if data.get("reasoning") is not None
        else None
    )

    return InitSelection(
        provider=provider,
        model_api=model_api,
        supports_vision=bool(data.get("supports_vision", False)),
        reasoning=reasoning,
        base_url=base_url,
        api_key=str(data.get("api_key") or API_KEY_PLACEHOLDER).strip() or API_KEY_PLACEHOLDER,
        model=model,
        identity=str(data.get("identity") or _default_identity_markdown()),
    )


def build_setup_schema() -> dict[str, Any]:
    """Return wizard metadata for the web client."""
    return {
        "providers": [
            {
                "id": provider,
                "label": _PROVIDER_LABELS.get(provider, provider),
                "description": _PROVIDER_DESCRIPTIONS.get(provider, ""),
            }
            for provider in KNOWN_PROVIDERS
        ],
        "models": {
            PROVIDER_OPENAI: list(OPENAI_MODELS),
            PROVIDER_ANTHROPIC: list(ANTHROPIC_MODELS),
            PROVIDER_DEEPSEEK: list(DEEPSEEK_MODELS),
            PROVIDER_QWEN: list(QWEN_MODELS),
            PROVIDER_CUSTOM: [],
        },
        "provider_base_urls": {
            PROVIDER_OPENAI: OPENAI_BASE_URL,
            PROVIDER_DEEPSEEK: DEEPSEEK_BASE_URL,
            PROVIDER_ANTHROPIC: ANTHROPIC_BASE_URL,
            PROVIDER_QWEN: QWEN_BASE_URL,
            PROVIDER_CUSTOM: CUSTOM_OPENAI_BASE_URL_PLACEHOLDER,
        },
        "custom_model_apis": [
            MODEL_API_OPENAI_CHAT_COMPLETIONS,
            MODEL_API_OPENAI_RESPONSES,
            MODEL_API_ANTHROPIC_MESSAGES,
        ],
        "reasoning": {
            "providers": {
                provider: reasoning_capability(provider).to_dict()
                for provider in KNOWN_PROVIDERS
                if provider != PROVIDER_CUSTOM
            },
            "custom_model_apis": {
                model_api: reasoning_capability(PROVIDER_CUSTOM, model_api).to_dict()
                for model_api in (
                    MODEL_API_OPENAI_CHAT_COMPLETIONS,
                    MODEL_API_OPENAI_RESPONSES,
                    MODEL_API_ANTHROPIC_MESSAGES,
                )
            },
        },
        "defaults": {
            "identity": _default_identity_markdown(),
        },
        "placeholders": {
            "api_key": API_KEY_PLACEHOLDER,
            "model": MODEL_PLACEHOLDER,
        },
        "name_pattern": "^[a-z][a-z0-9_-]*$",
    }


SETUP_CHANNELS = frozenset({"voice", "feishu", "weixin"})


class ChannelSetupError(ValueError):
    """Raised when channel setup preconditions fail or config cannot be written."""


def _channel_configured(config: dict[str, Any], channel: str) -> bool:
    channels_cfg = config.get("channels")
    if not isinstance(channels_cfg, dict):
        return False
    data = channels_cfg.get(channel)
    if not isinstance(data, dict):
        return False
    if channel == "voice":
        return bool(data)
    if channel == "feishu":
        return bool(data.get("app_id") and data.get("app_secret"))
    if channel == "weixin":
        return bool(data.get("account_id"))
    return False


def build_voice_setup_schema(config: dict[str, Any]) -> dict[str, Any]:
    """Return wizard metadata for the web voice channel setup client."""
    return {
        "defaults": {"voice_enabled": True, "voice_api_key": ""},
        "placeholders": {"soniox_api_key": SONIOX_KEY_PLACEHOLDER},
        "configured": _channel_configured(config, "voice"),
        "can_force": True,
    }


def build_feishu_setup_schema(config: dict[str, Any]) -> dict[str, Any]:
    """Return wizard metadata for the web Feishu channel setup client.

    Interactive setup is credentials-only. Behavior knobs
    (``stream``, ``group_fetch_limit``, ``group_reply_only_when_mentioned``)
    ship as recommended silent defaults — matching CLI Feishu setup.
    """
    return {
        "credential_modes": [
            {
                "id": "one_click",
                "label": "Create new Feishu app",
                "description": "Recommended. Create a new Feishu app and authorize it.",
            },
            {
                "id": "manual",
                "label": "Use existing App ID / App Secret",
                "description": "Paste credentials from an app you already created in the Feishu developer console.",
            },
        ],
        "defaults": {
            "credential_mode": "one_click",
            "stream": False,
            "group_fetch_limit": 10,
            "group_reply_only_when_mentioned": False,
        },
        "configured": _channel_configured(config, "feishu"),
        "can_force": True,
    }


def build_weixin_setup_schema(config: dict[str, Any]) -> dict[str, Any]:
    """Return wizard metadata for the web Weixin channel setup client."""
    from ...integrations.weixin.config import ILINK_BASE_URL, WEIXIN_CDN_BASE_URL

    return {
        "defaults": {
            "base_url": ILINK_BASE_URL,
            "cdn_base_url": WEIXIN_CDN_BASE_URL,
            "owner_only": True,
            "allow_users": [],
            "media_enabled": True,
        },
        "configured": _channel_configured(config, "weixin"),
        "can_force": True,
    }


def build_channel_setup_schema(channel: str, config: dict[str, Any]) -> dict[str, Any]:
    """Return wizard metadata for a specific channel setup flow."""
    normalized = str(channel or "").strip().lower()
    if normalized == "voice":
        return build_voice_setup_schema(config)
    if normalized == "feishu":
        return build_feishu_setup_schema(config)
    if normalized == "weixin":
        return build_weixin_setup_schema(config)
    raise ChannelSetupError(f"Unknown channel: {channel}")


def voice_init_selection_from_mapping(
    data: Mapping[str, Any],
    *,
    config: dict[str, Any],
) -> VoiceInitSelection:
    """Build a ``VoiceInitSelection`` from API/JSON input."""
    _ = config
    removed_fields = {
        "voice_provider",
        "voice_stt_provider",
        "voice_stt_api_key",
        "voice_tts_provider",
        "voice_tts_api_key",
        "voice_enable_interruptions",
        "voice_wake_enabled",
        "voice_wake_phrases",
        "voice_exit_phrases",
    }.intersection(data)
    if removed_fields:
        fields = ", ".join(sorted(removed_fields))
        raise ChannelSetupError(
            f"Voice setup is Soniox-only; removed fields are not accepted: {fields}"
        )
    return VoiceInitSelection(
        voice_enabled=bool(data.get("voice_enabled", True)),
        voice_api_key=str(data.get("voice_api_key") or "").strip(),
    )


def feishu_init_selection_from_mapping(data: Mapping[str, Any]) -> FeishuInitSelection:
    """Build a ``FeishuInitSelection`` from API/JSON input."""
    credential_mode = str(data.get("credential_mode") or "manual").strip() or "manual"
    if credential_mode not in {"one_click", "manual"}:
        raise ChannelSetupError("credential_mode must be one of: one_click, manual")

    app_id = str(data.get("app_id") or "").strip()
    app_secret = str(data.get("app_secret") or "").strip()
    if not app_id or not app_secret:
        raise ChannelSetupError("app_id and app_secret are required")

    group_fetch_limit = int(data.get("group_fetch_limit", 10))
    if group_fetch_limit < 0:
        raise ChannelSetupError("group_fetch_limit must be >= 0")

    return FeishuInitSelection(
        app_id=app_id,
        app_secret=app_secret,
        stream=bool(data.get("stream", False)),
        group_fetch_limit=group_fetch_limit,
        group_reply_only_when_mentioned=bool(data.get("group_reply_only_when_mentioned", False)),
        credential_mode=credential_mode,
    )


def weixin_init_selection_from_mapping(data: Mapping[str, Any]) -> WeixinInitSelection:
    """Build a ``WeixinInitSelection`` from API/JSON input."""
    from ...integrations.weixin.config import ILINK_BASE_URL, WEIXIN_CDN_BASE_URL

    account_id = str(data.get("account_id") or "").strip()
    owner_user_id = str(data.get("owner_user_id") or "").strip()
    if not account_id or not owner_user_id:
        raise ChannelSetupError("account_id and owner_user_id are required")

    base_url = str(data.get("base_url") or ILINK_BASE_URL).strip().rstrip("/") or ILINK_BASE_URL
    cdn_base_url = str(data.get("cdn_base_url") or WEIXIN_CDN_BASE_URL).strip().rstrip("/") or WEIXIN_CDN_BASE_URL
    allow_users = _normalize_repeated_values(data.get("allow_users"))

    return WeixinInitSelection(
        account_id=account_id,
        owner_user_id=owner_user_id,
        base_url=base_url,
        cdn_base_url=cdn_base_url,
        owner_only=bool(data.get("owner_only", True)),
        allow_users=allow_users,
        media_enabled=bool(data.get("media_enabled", True)),
    )


def _load_agent_config_file(config_dir: Path) -> tuple[Path, dict[str, Any]]:
    config_file = config_dir / BaseAgentConfig.CONFIG_FILENAME
    if not config_file.is_file():
        raise ChannelSetupError(
            f"Config not found: {config_file}. Create an agent first, then return to channel setup."
        )
    try:
        with config_file.open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        raise ChannelSetupError(f"Invalid YAML in {config_file}: {exc}") from exc
    if not isinstance(config, dict):
        raise ChannelSetupError(f"Configuration must be a mapping: {config_file}")
    return config_file, config


def apply_channel_setup(
    *,
    channel: str,
    config_dir: Path,
    selection_data: Mapping[str, Any],
    force: bool = False,
) -> dict[str, Any]:
    """Apply channel setup to an existing agent config directory."""
    normalized = str(channel or "").strip().lower()
    if normalized not in SETUP_CHANNELS:
        raise ChannelSetupError(f"Unknown channel: {channel}")

    config_dir = config_dir.expanduser().resolve()
    config_file, config = _load_agent_config_file(config_dir)
    channels_cfg = config.setdefault("channels", {})
    if not isinstance(channels_cfg, dict):
        raise ChannelSetupError("channels must be a dictionary")

    if normalized in channels_cfg and not force:
        raise ChannelSetupError(
            f"channels.{normalized} already exists. Set force=true to overwrite."
        )

    if normalized == "voice":
        selection = voice_init_selection_from_mapping(selection_data, config=config)
        if not selection.voice_enabled:
            channels_cfg.pop("voice", None)
        else:
            existing_voice = channels_cfg.get("voice")
            channels_cfg["voice"] = _voice_channel_config(
                selection,
                existing=existing_voice if isinstance(existing_voice, dict) else None,
            )
    elif normalized == "feishu":
        selection = feishu_init_selection_from_mapping(selection_data)
        _ensure_api_port(channels_cfg)
        channels_cfg["feishu"] = _feishu_channel_config(selection)
    else:
        selection = weixin_init_selection_from_mapping(selection_data)
        credentials_data = selection_data.get("credentials")
        if isinstance(credentials_data, dict):
            from ...integrations.weixin.state import WeixinCredentials, WeixinStateStore

            store = WeixinStateStore(config_dir)
            store.save_credentials(WeixinCredentials.from_dict(credentials_data))
        _ensure_api_port(channels_cfg)
        channels_cfg["weixin"] = _weixin_channel_config(selection)

    config_file.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=False), encoding="utf-8")
    return {
        "channel": normalized,
        "config_path": str(config_file),
        "configured": True,
    }


def _default_init_selection() -> InitSelection:
    return InitSelection(
        provider="openai",
        base_url=OPENAI_BASE_URL,
        api_key=API_KEY_PLACEHOLDER,
        model="gpt-5.6-terra",
        identity=_default_identity_markdown(),
    )


def _voice_channel_config(
    selection: VoiceInitSelection,
    *,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Update credentials while preserving valid hand-written flat options."""
    preserved_keys = {
        "voice",
        "language_hints",
        "fallback_language",
        "speed",
        "context",
        "audio",
    }
    voice_config = {
        key: value
        for key, value in (existing or {}).items()
        if key in preserved_keys
    }
    return {
        "api_key": selection.voice_api_key.strip() or SONIOX_KEY_PLACEHOLDER,
        **voice_config,
    }


def _config_yaml(selection: InitSelection, port: int) -> str:
    provider_config = {
        "name": selection.provider,
        "base_url": selection.base_url,
        "api_key": selection.api_key,
        "model": selection.model,
    }
    if selection.provider == PROVIDER_CUSTOM:
        provider_config["model_api"] = selection.model_api or MODEL_API_OPENAI_CHAT_COMPLETIONS
        provider_config["supports_vision"] = selection.supports_vision
    if selection.reasoning is not None:
        provider_config["reasoning"] = selection.reasoning.to_dict()

    config = {
        "provider": provider_config,
        "agent": {
            "max_history": AgentConfig.DEFAULT_MAX_HISTORY,
            "journal_batch_size": AgentConfig.JOURNAL_BATCH_SIZE,
            "max_iter": AgentConfig.DEFAULT_MAX_ITER,
            "max_concurrent_tools": AgentConfig.DEFAULT_MAX_CONCURRENT_TOOLS,
            "subconscious_activity": AgentConfig.SUBCONSCIOUS_ACTIVITY,
            "memory_recent_days": AgentConfig.MEMORY_RECENT_DAYS,
        },
        "channels": {
            "api": {
                "host": BaseAgentConfig.DEFAULT_HOST,
                "port": port,
            }
        },
        "web": {
            "api_url": f"http://127.0.0.1:{port}",
        },
    }
    config["search"] = {"provider": "none"}
    config["image_generation"] = {"provider": "none"}
    yaml_str = yaml.safe_dump(config, sort_keys=False, allow_unicode=False)
    yaml_str = yaml_str.replace(
        f"max_history: {AgentConfig.DEFAULT_MAX_HISTORY}\n",
        f"max_history: {AgentConfig.DEFAULT_MAX_HISTORY}  # Prompt hot window (raw conversation messages).\n",
    )
    yaml_str = yaml_str.replace(
        f"journal_batch_size: {AgentConfig.JOURNAL_BATCH_SIZE}\n",
        f"journal_batch_size: {AgentConfig.JOURNAL_BATCH_SIZE}  # Diary maintenance batch size; independent of max_history.\n",
    )
    # Inline comment for subconscious_activity
    yaml_str = yaml_str.replace(
        "subconscious_activity: 0.02\n",
        "subconscious_activity: 0.02  # 0=off, 1=very active. Suggested: 0.01~0.1\n",
    )
    yaml_str = yaml_str.replace(
        "memory_recent_days: 2\n",
        "memory_recent_days: 2  # Days of diary injected each turn; 0 disables injection.\n",
    )
    return yaml_str


def _default_identity_markdown() -> str:
    return """# Identity

You are a practical collaborator with your own continuing identity.
Answer clearly, adapt to the user's language, and decide what to share or keep private by your own judgment.
"""


def _edit_later_identity_markdown() -> str:
    return """# Identity

Describe this agent's role, tone, and behavior here.
"""


def _format_identity_markdown(identity: str) -> str:
    identity = identity.strip()
    if not identity:
        return _edit_later_identity_markdown()
    if identity.startswith("#"):
        return identity + "\n"
    return f"# Identity\n\n{identity}\n"


def _prompt_text(
    prompt: str,
    *,
    default: Optional[str] = None,
    input_func: Callable[[str], str] = input,
) -> str:
    suffix = f" [{default}]" if default else ""
    value = input_func(f"{prompt}{suffix}: ").strip()
    if not value and default is not None:
        return default
    return value


def _prompt_yes_no(
    prompt: str,
    *,
    default: bool = False,
    input_func: Callable[[str], str] = input,
) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        value = input_func(f"{prompt}{suffix}: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Please answer y or n.")


def _select_option(
    title: str,
    options: Sequence[str],
    *,
    default_index: int = 0,
    input_func: Callable[[str], str] = input,
) -> str:
    print(f"\n{title}")
    for index, option in enumerate(options, 1):
        print(f"  {index}. {option}")

    while True:
        raw_choice = input_func("Choose an option number: ").strip()
        if not raw_choice:
            return options[default_index]
        if raw_choice.isdigit():
            choice = int(raw_choice)
            if 1 <= choice <= len(options):
                return options[choice - 1]
        print(f"Please enter a number from 1 to {len(options)}.")


def _model_options(options: Sequence[str]) -> tuple[str, ...]:
    if CUSTOM_MODEL_OPTION in options:
        return tuple(options)

    values = list(options)
    values.append(CUSTOM_MODEL_OPTION)
    return tuple(values)


def _resolve_selected_model(
    selected_model: str,
    *,
    prompt_text: Callable[[str, Optional[str]], str],
) -> str:
    if selected_model == CUSTOM_MODEL_OPTION:
        custom_model = prompt_text("Custom model name", MODEL_PLACEHOLDER).strip()
        return custom_model or MODEL_PLACEHOLDER
    if selected_model == "Decide later":
        return MODEL_PLACEHOLDER
    return selected_model


def _prompt_voice_api_key(
    *,
    secret_input_func: Callable[[str], str] = getpass.getpass,
) -> str:
    api_key = secret_input_func(
        "Soniox API key for voice (leave blank to fill in later): "
    ).strip()
    return api_key or SONIOX_KEY_PLACEHOLDER


def _select_custom_model_api(
    *,
    input_func: Callable[[str], str] = input,
) -> str:
    return _select_option(
        "Custom provider model API",
        (
            MODEL_API_OPENAI_CHAT_COMPLETIONS,
            MODEL_API_OPENAI_RESPONSES,
            MODEL_API_ANTHROPIC_MESSAGES,
        ),
        default_index=0,
        input_func=input_func,
    )


def _prompt_multiline_identity(input_func: Callable[[str], str] = input) -> str:
    print("\nEnter agent identity, or leave blank to edit later.")
    print("Type '.' on a new line to save.\n")
    lines = []
    while True:
        line = input_func("> ")
        if line.strip() == ".":
            break
        lines.append(line)
    return _format_identity_markdown("\n".join(lines))


def _menu_option_rows(options: Sequence[str], descriptions: Optional[dict[str, str]] = None) -> list[MenuOption]:
    option_descriptions = descriptions or {}
    rows: list[MenuOption] = []
    for option in options:
        rows.append(
            MenuOption(
                key=option,
                title=option,
                description=option_descriptions.get(option, f"Use {option}."),
            )
        )
    return rows


def _model_option_rows(options: Sequence[str], descriptions: Optional[dict[str, str]] = None) -> list[MenuOption]:
    option_descriptions = dict(descriptions or {})
    option_descriptions[CUSTOM_MODEL_OPTION] = "Enter a custom model name now."
    return _menu_option_rows(_model_options(options), option_descriptions)


def _terminal_select_option(
    ui: TerminalUI,
    title: str,
    options: Sequence[str],
    *,
    descriptions: Optional[dict[str, str]] = None,
    default_index: int = 0,
    subtitle: str = "",
) -> str:
    choice = ui.select(
        label=title,
        subtitle=subtitle,
        options=_menu_option_rows(options, descriptions),
        default_index=default_index,
    )
    if choice is None:
        raise KeyboardInterrupt()
    return choice.key


def _select_model_option(
    title: str,
    options: Sequence[str],
    *,
    default_index: int = 0,
    input_func: Callable[[str], str] = input,
) -> str:
    return _resolve_selected_model(
        _select_option(
            title,
            _model_options(options),
            default_index=default_index,
            input_func=input_func,
        ),
        prompt_text=lambda prompt, default: _prompt_text(
            prompt,
            default=default,
            input_func=input_func,
        ),
    )


def _terminal_select_model_option(
    ui: TerminalUI,
    title: str,
    options: Sequence[str],
    *,
    descriptions: Optional[dict[str, str]] = None,
    default_index: int = 0,
    subtitle: str = "",
) -> str:
    choice = ui.select(
        label=title,
        subtitle=subtitle,
        options=_model_option_rows(options, descriptions),
        default_index=default_index,
    )
    if choice is None:
        raise KeyboardInterrupt()
    return _resolve_selected_model(
        choice.key,
        prompt_text=lambda prompt, default: _terminal_prompt_text(ui, prompt, default=default),
    )


def _terminal_prompt_text(ui: TerminalUI, prompt: str, *, default: Optional[str] = None) -> str:
    return ui.ask_text(prompt, default=default)


def _terminal_prompt_yes_no(ui: TerminalUI, prompt: str, *, default: bool = False) -> bool:
    result = ui.confirm(prompt, default=default)
    if result is None:
        raise KeyboardInterrupt()
    return result


def _terminal_prompt_multiline_identity(ui: TerminalUI) -> str:
    text = ui.ask_text(
        "Identity",
        default="Describe the agent's role and tone.",
    )
    if not text:
        text = ""
    return _format_identity_markdown(text)


@dataclass(frozen=True)
class InitPromptSurface:
    """Prompt callbacks shared by terminal and plain init flows."""

    select_option: Callable[..., str]
    select_model_option: Callable[..., str]
    select_custom_model_api: Callable[[], str]
    prompt_text: Callable[..., str]
    prompt_yes_no: Callable[..., bool]
    ask_secret: Callable[[str], str]
    prompt_multiline_identity: Callable[[], str]
    on_start: Callable[[], None] = lambda: None
    reasoning_prompt_enabled: bool = False


def _collect_reasoning_config(
    surface: InitPromptSurface,
    *,
    provider: str,
    model_api: str,
) -> Optional[ReasoningConfig]:
    capability = reasoning_capability(provider, model_api or None)
    if not capability.supported:
        return None

    mode = surface.select_option(
        "Reasoning mode",
        ("automatic", "enabled"),
        descriptions={
            "automatic": "Follow the model default without sending reasoning controls (recommended).",
            "enabled": "Enable reasoning with an explicit strength.",
        },
        default_index=0,
    )
    if mode == "automatic":
        return None

    control = capability.controls[0]
    if len(capability.controls) > 1:
        control = surface.select_option(
            "Reasoning strength control",
            capability.controls,
            descriptions={
                "effort": "Use the provider's adaptive discrete effort level.",
                "budget_tokens": "Use a fixed thinking-token budget for older models.",
            },
            default_index=0,
        )
    if control == "effort":
        default_index = (
            capability.effort_values.index("medium")
            if "medium" in capability.effort_values
            else 0
        )
        effort = surface.select_option(
            "Reasoning effort",
            capability.effort_values,
            default_index=default_index,
        )
        return ReasoningConfig(enabled=True, effort=effort)

    minimum = capability.min_budget_tokens or 1
    default_budget = max(minimum, 4096)
    raw_budget = surface.prompt_text(
        "Reasoning token budget",
        default=str(default_budget),
    ).strip()
    try:
        budget_tokens = int(raw_budget)
    except ValueError as exc:
        raise ValueError("Reasoning token budget must be an integer") from exc
    if budget_tokens < minimum:
        raise ValueError(f"Reasoning token budget must be at least {minimum}")
    return ReasoningConfig(enabled=True, budget_tokens=budget_tokens)


def _collect_init_selection_core(surface: InitPromptSurface) -> InitSelection:
    surface.on_start()

    provider = surface.select_option(
        "Provider",
        KNOWN_PROVIDERS,
        descriptions={
            PROVIDER_OPENAI: "GPT family via the OpenAI platform.",
            PROVIDER_DEEPSEEK: "DeepSeek chat and coding models.",
            PROVIDER_QWEN: "Qwen models via DashScope-compatible APIs.",
            PROVIDER_ANTHROPIC: "Claude models via Anthropic Messages.",
            PROVIDER_CUSTOM: "Bring your own OpenAI, Responses, or Anthropic endpoint.",
        },
        subtitle="Choose the model provider to configure.",
    )
    model_api = ""
    supports_vision = False

    if provider == PROVIDER_OPENAI:
        selected_model = surface.select_model_option(
            "OpenAI Model",
            OPENAI_MODELS,
            descriptions={
                "gpt-5.6-terra": "Recommended everyday default — balance of quality and cost.",
                "gpt-5.6-luna": "Cost-sensitive / high-volume (heartbeat, diary, light chat).",
                "gpt-5.6-sol": "Frontier capability for complex reasoning and coding.",
            },
            default_index=0,
        )
        base_url = OPENAI_BASE_URL
    elif provider == PROVIDER_ANTHROPIC:
        selected_model = surface.select_model_option(
            "Anthropic Model",
            ANTHROPIC_MODELS,
            default_index=0,
        )
        base_url = ANTHROPIC_BASE_URL
    elif provider == PROVIDER_DEEPSEEK:
        selected_model = surface.select_model_option(
            "DeepSeek Model",
            DEEPSEEK_MODELS,
            default_index=0,
        )
        base_url = DEEPSEEK_BASE_URL
    elif provider == PROVIDER_QWEN:
        selected_model = surface.select_model_option(
            "Qwen Model",
            QWEN_MODELS,
            default_index=1,
        )
        base_url = QWEN_BASE_URL
    else:
        model_api = surface.select_custom_model_api()
        selected_model = MODEL_PLACEHOLDER
        default_base_url = (
            CUSTOM_ANTHROPIC_BASE_URL_PLACEHOLDER
            if model_api == MODEL_API_ANTHROPIC_MESSAGES
            else CUSTOM_OPENAI_BASE_URL_PLACEHOLDER
        )
        base_url = surface.prompt_text(
            "Custom provider base URL",
            default=default_base_url,
        )

    model = selected_model
    api_key = surface.ask_secret("API key (leave blank to fill in later): ").strip() or API_KEY_PLACEHOLDER
    # Create stays minimal: reasoning / vision knobs live in Edit Setup later.
    reasoning = None
    if surface.reasoning_prompt_enabled:
        reasoning = _collect_reasoning_config(
            surface,
            provider=provider,
            model_api=model_api,
        )

    identity = surface.prompt_multiline_identity()

    return InitSelection(
        provider=provider,
        model_api=model_api,
        supports_vision=supports_vision,
        base_url=base_url,
        api_key=api_key,
        model=model,
        identity=identity,
        reasoning=reasoning,
    )


def collect_init_selection_terminal_ui(
    *,
    ui: Optional[TerminalUI] = None,
    secret_input_func: Callable[[str], str] = getpass.getpass,
) -> InitSelection:
    wizard_ui = ui or TerminalUI()
    ask_secret = wizard_ui.ask_secret if wizard_ui.interactive else secret_input_func

    def select_option(title, options, **kwargs):
        return _terminal_select_option(wizard_ui, title, options, **kwargs)

    def select_model_option(title, models, **kwargs):
        return _terminal_select_model_option(wizard_ui, title, models, **kwargs)

    def select_custom_model_api() -> str:
        return _terminal_select_option(
            wizard_ui,
            "Custom Provider Model API",
            (
                MODEL_API_OPENAI_CHAT_COMPLETIONS,
                MODEL_API_OPENAI_RESPONSES,
                MODEL_API_ANTHROPIC_MESSAGES,
            ),
            default_index=0,
            subtitle="Select the wire protocol your custom provider speaks.",
        )

    def prompt_text(label: str, *, default: str = "") -> str:
        return _terminal_prompt_text(wizard_ui, label, default=default)

    def prompt_yes_no(label: str, *, default: bool = False) -> bool:
        return _terminal_prompt_yes_no(wizard_ui, label, default=default)

    return _collect_init_selection_core(
        InitPromptSurface(
            select_option=select_option,
            select_model_option=select_model_option,
            select_custom_model_api=select_custom_model_api,
            prompt_text=prompt_text,
            prompt_yes_no=prompt_yes_no,
            ask_secret=ask_secret,
            prompt_multiline_identity=lambda: _terminal_prompt_multiline_identity(wizard_ui),
            reasoning_prompt_enabled=False,
        )
    )


def collect_init_selection(
    *,
    input_func: Callable[[str], str] = input,
    secret_input_func: Callable[[str], str] = getpass.getpass,
) -> InitSelection:
    def on_start() -> None:
        print("\nxAgent init")
        print("Configure the runtime first; files will be written after these choices.")

    def select_option(title, options, **kwargs):
        kwargs.pop("descriptions", None)
        kwargs.pop("subtitle", None)
        kwargs.setdefault("input_func", input_func)
        return _select_option(title, options, **kwargs)

    def select_model_option(title, models, **kwargs):
        kwargs.pop("descriptions", None)
        kwargs.setdefault("input_func", input_func)
        return _select_model_option(title, models, **kwargs)

    def select_custom_model_api() -> str:
        return _select_custom_model_api(input_func=input_func)

    def prompt_text(label: str, *, default: str = "") -> str:
        return _prompt_text(label, default=default, input_func=input_func)

    def prompt_yes_no(label: str, *, default: bool = False) -> bool:
        return _prompt_yes_no(label, default=default, input_func=input_func)

    return _collect_init_selection_core(
        InitPromptSurface(
            select_option=select_option,
            select_model_option=select_model_option,
            select_custom_model_api=select_custom_model_api,
            prompt_text=prompt_text,
            prompt_yes_no=prompt_yes_no,
            ask_secret=secret_input_func,
            prompt_multiline_identity=lambda: _prompt_multiline_identity(input_func=input_func),
            on_start=on_start,
        )
    )


def init_agent_directory(
    config_dir: Optional[str] = None,
    *,
    force: bool = False,
    selection: Optional[InitSelection] = None,
    clear_runtime_data: bool = False,
    quiet: bool = False,
    registry_root: Optional[Path] = None,
) -> InitResult:
    resolved_dir = Path(config_dir or BaseAgentConfig.DEFAULT_CONFIG_DIR).expanduser().resolve()
    resolved_dir.mkdir(parents=True, exist_ok=True)
    config_file = resolved_dir / BaseAgentConfig.CONFIG_FILENAME
    identity_file = resolved_dir / BaseAgentConfig.IDENTITY_FILENAME
    memory_dir = resolved_dir / BaseAgentConfig.MEMORY_DIRNAME
    messages_dir = resolved_dir / BaseAgentConfig.MESSAGE_DIRNAME
    workspace_dir = resolved_dir / BaseAgentConfig.WORKSPACE_DIRNAME
    skills_dir = resolved_dir / BaseAgentConfig.SKILLS_DIRNAME
    tasks_dir = resolved_dir / BaseAgentConfig.TASKS_DIRNAME
    managed_paths = (config_file, identity_file)
    runtime_dirs = (memory_dir, messages_dir, workspace_dir, skills_dir, tasks_dir)
    conflicts = tuple(path for path in managed_paths if path.exists())

    if conflicts and not force:
        if not quiet:
            TerminalUI().print_panel(
                "\n".join([
                    "xAgent init found existing managed files.",
                    *(f"Existing: {path}" for path in conflicts),
                    "Re-run with --force to overwrite config.yaml and identity.md.",
                ]),
                title="Init Stopped",
            )
        return InitResult(
            config_path=config_file,
            identity_path=identity_file,
            memory_dir=memory_dir,
            messages_dir=messages_dir,
            workspace_dir=workspace_dir,
            skills_dir=skills_dir,
            tasks_dir=tasks_dir,
            wrote_files=False,
            conflicts=conflicts,
        )

    if clear_runtime_data:
        for runtime_dir in runtime_dirs:
            _clear_runtime_directory(runtime_dir)
    memory_dir.mkdir(parents=True, exist_ok=True)
    messages_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    skills_dir.mkdir(parents=True, exist_ok=True)
    tasks_dir.mkdir(parents=True, exist_ok=True)

    selection = selection or _default_init_selection()
    port = allocate_api_port(root=registry_root)
    config_file.write_text(_config_yaml(selection, port=port), encoding="utf-8")
    identity_file.write_text(selection.identity, encoding="utf-8")

    if not quiet:
        TerminalUI().print_panel(
            "\n".join([
                "xAgent project files written successfully.",
                f"Config: {config_file}",
                f"Identity: {identity_file}",
                f"Memory: {memory_dir}",
                f"Messages: {messages_dir}",
                f"Workspace: {workspace_dir}",
                f"Skills: {skills_dir}",
                f"Tasks: {tasks_dir}",
            ]),
            title="xAgent Ready",
            leading_blank_line=True,
        )
    return InitResult(
        config_path=config_file,
        identity_path=identity_file,
        memory_dir=memory_dir,
        messages_dir=messages_dir,
        workspace_dir=workspace_dir,
        skills_dir=skills_dir,
        tasks_dir=tasks_dir,
        wrote_files=True,
        conflicts=(),
    )


def _clear_runtime_directory(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _format_init_command(command: str, *, config_dir: Path | None = None, agent_name: str | None = None) -> str:
    del config_dir
    if agent_name:
        return f"{command} --agent {shlex.quote(agent_name)}"
    return command


def _print_init_next_steps(*, config_dir: Path, agent_name: str | None = None) -> None:
    ready_now = [
        (
            "chat",
            _format_init_command("xagent chat", config_dir=config_dir, agent_name=agent_name),
            "Talk to the agent in your terminal.",
        ),
        (
            "web",
            _format_init_command("xagent web start", config_dir=config_dir, agent_name=agent_name),
            "Start the browser web client (requires a running api channel).",
        ),
        (
            "api",
            _format_init_command("xagent api start", config_dir=config_dir, agent_name=agent_name),
            "Run the HTTP / SSE / WebSocket channel in the background.",
        ),
    ]

    voice_init = _format_init_command("xagent voice setup", config_dir=config_dir, agent_name=agent_name)
    voice_start = _format_init_command("xagent voice start", config_dir=config_dir, agent_name=agent_name)
    feishu_init = _format_init_command("xagent feishu setup", config_dir=config_dir, agent_name=agent_name)
    feishu_start = _format_init_command("xagent feishu start", config_dir=config_dir, agent_name=agent_name)
    weixin_init = _format_init_command("xagent weixin setup", config_dir=config_dir, agent_name=agent_name)
    weixin_start = _format_init_command("xagent weixin start", config_dir=config_dir, agent_name=agent_name)

    content = Text()
    content.append("Pick how you want to use it next.\n\n")
    content.append("Ready now:\n")
    for name, command, description in ready_now:
        content.append(f"{name:<7} ", style="")
        content.append(command, style="cyan")
        content.append(f"\n        {description}\n")
    content.append("\nOptional:\n")
    content.append("voice   ", style="")
    content.append(voice_init, style="cyan")
    content.append("\n        Configure the microphone/speaker channel, then start it with ")
    content.append(voice_start, style="cyan")
    content.append(".")
    content.append("\n")
    content.append("feishu  ", style="")
    content.append(feishu_init, style="cyan")
    content.append("\n        Create a Feishu bot config, then start it with ")
    content.append(feishu_start, style="cyan")
    content.append(".")
    content.append("\n")
    content.append("weixin  ", style="")
    content.append(weixin_init, style="cyan")
    content.append("\n        Scan WeChat to configure the DM channel, then start it with ")
    content.append(weixin_start, style="cyan")
    content.append(".")

    TerminalUI().print_panel(content, title="Next Steps")


def _weixin_access_label(selection: WeixinInitSelection) -> str:
    if selection.owner_only:
        extra = len(selection.allow_users)
        if extra:
            return f"Owner + {extra} allowlisted user(s)"
        return "Owner only"
    if selection.allow_users:
        return "Allowlist only"
    return "All direct messages"


def handle_init(args: argparse.Namespace) -> int:
    resolved_dir = _setup_runtime_dir(args)
    conflicts = tuple(
        path for path in (
            resolved_dir / BaseAgentConfig.CONFIG_FILENAME,
            resolved_dir / BaseAgentConfig.IDENTITY_FILENAME,
        )
        if path.exists()
    )
    if conflicts and not args.force:
        result = init_agent_directory(
            str(resolved_dir),
            force=args.force,
        )
        return 0 if result.wrote_files else 1

    clear_runtime_data = False
    ui = TerminalUI()
    try:
        if args.force:
            clear_runtime_data = _terminal_prompt_yes_no(
                ui,
                "Clear existing memory/, messages/, workspace/, tasks/, and skills/ data as part of init --force?",
                default=False,
            )

        selection = collect_init_selection_terminal_ui(ui=ui)
    except KeyboardInterrupt:
        ui.print_panel("Init cancelled before writing files.", title="Init Cancelled")
        return 1

    result = init_agent_directory(
        str(resolved_dir),
        force=args.force,
        selection=selection,
        clear_runtime_data=clear_runtime_data,
    )
    if result.wrote_files:
        _print_init_next_steps(
            config_dir=result.config_path.parent,
            agent_name=getattr(args, "agent", None),
        )
    return 0 if result.wrote_files else 1


def _arg_text(args: argparse.Namespace, name: str) -> str:
    return str(getattr(args, name, "") or "").strip()


def collect_voice_init_selection_terminal_ui(
    *,
    args: argparse.Namespace,
    config: dict[str, Any],
    ui: Optional[TerminalUI] = None,
    secret_input_func: Callable[[str], str] = getpass.getpass,
) -> VoiceInitSelection:
    wizard_ui = ui or TerminalUI()
    ask_secret = wizard_ui.ask_secret if wizard_ui.interactive else secret_input_func
    _ = config
    enabled_arg = getattr(args, "enabled", None)
    voice_enabled = (
        _terminal_prompt_yes_no(wizard_ui, "Enable voice?", default=True)
        if enabled_arg is None
        else bool(enabled_arg)
    )
    voice_api_key = _arg_text(args, "api_key")
    if voice_enabled and not voice_api_key:
        voice_api_key = _prompt_voice_api_key(secret_input_func=ask_secret)
    return VoiceInitSelection(voice_enabled=voice_enabled, voice_api_key=voice_api_key)


def _print_voice_post_setup(
    config_path: Path,
    selection: VoiceInitSelection,
    *,
    agent_name: str | None = None,
) -> None:
    config_dir = config_path.parent
    ui = TerminalUI()
    summary = Text()
    summary.append(f"Voice channel updated in {config_path}\n\n")
    summary.append("Configured behavior:\n")
    if selection.voice_enabled:
        summary.append("- Provider: Soniox\n")
        summary.append("- Mode: Half-duplex\n")
    else:
        summary.append("- Voice: Disabled\n")
    ui.print_panel(summary, title="Voice Ready", leading_blank_line=True)

    start = _format_init_command("xagent voice start", config_dir=config_dir, agent_name=agent_name)
    status = _format_init_command("xagent voice status", config_dir=config_dir, agent_name=agent_name)
    logs = _format_init_command("xagent voice logs -f", config_dir=config_dir, agent_name=agent_name)
    next_steps = Text()
    next_steps.append("Run next:\n")
    next_steps.append("start   ")
    next_steps.append(start, style="cyan")
    next_steps.append("\n        Start only the voice channel.\n")
    next_steps.append("status  ")
    next_steps.append(status, style="cyan")
    next_steps.append("\n        Check PID, logs, and whether the channel is running.\n")
    next_steps.append("logs    ")
    next_steps.append(logs, style="cyan")
    next_steps.append("\n        Follow the voice channel log live.\n")
    ui.print_panel(next_steps, title="Next Steps")


def _setup_exit_on_cancel(ui: TerminalUI, args: argparse.Namespace, *, channel: str) -> int:
    display_name = channel.capitalize()
    if getattr(args, "show_intro", True):
        ui.print_panel(
            f"{display_name} setup cancelled before writing config.",
            title=f"{display_name} Setup Cancelled",
        )
        return 1
    return SETUP_EXIT_CANCELLED


def _print_channel_setup_intro(
    ui: TerminalUI,
    *,
    channel: str,
    config_file: Path,
    replacing: bool = False,
    extra_lines: Sequence[str] = (),
) -> None:
    display_name = channel.capitalize()
    intro_lines = [
        f"Runtime: {config_file.parent}",
        f"Config: {config_file}",
    ]
    if replacing:
        intro_lines.append(f"Existing channels.{channel} settings will be replaced.")
    intro_lines.extend(extra_lines)
    ui.print_panel("\n".join(intro_lines), title=f"{display_name} Setup", leading_blank_line=True)


def handle_init_voice(args: argparse.Namespace) -> int:
    ui = TerminalUI()
    config_file = _config_path(args)
    agent_name = getattr(args, "agent", None)
    init_command = _format_init_command("xagent setup", config_dir=config_file.parent, agent_name=agent_name)
    if not config_file.is_file():
        ui.print_panel(
            f"Config not found: {config_file}\nRun {init_command} first, then return to Voice setup.",
            title="Voice Setup Stopped",
        )
        return 1

    try:
        with config_file.open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        ui.print_panel(f"Invalid YAML in {config_file}: {exc}", title="Voice Setup Stopped", border_style="red")
        return 1
    if not isinstance(config, dict):
        ui.print_panel(f"Configuration must be a mapping: {config_file}", title="Voice Setup Stopped", border_style="red")
        return 1

    channels_cfg = config.setdefault("channels", {})
    if not isinstance(channels_cfg, dict):
        ui.print_panel("channels must be a dictionary", title="Voice Setup Stopped", border_style="red")
        return 1
    if "voice" in channels_cfg and not getattr(args, "force", False):
        force_command = _format_init_command(
            "xagent voice setup --force",
            config_dir=config_file.parent,
            agent_name=agent_name,
        )
        ui.print_panel(
            f"channels.voice already exists in {config_file}.\nRun {force_command} to overwrite the Voice channel settings.",
            title="Voice Setup Stopped",
        )
        return 1

    _print_channel_setup_intro(
        ui,
        channel="voice",
        config_file=config_file,
        replacing="voice" in channels_cfg,
    )

    try:
        selection = collect_voice_init_selection_terminal_ui(args=args, config=config, ui=ui)
    except (KeyboardInterrupt, ReturnToLauncherHome):
        ui.print_panel("Voice setup cancelled before writing config.", title="Voice Setup Cancelled")
        return 1
    except ValueError as exc:
        ui.print_panel(str(exc), title="Voice Setup Stopped", border_style="red")
        return 1

    from dataclasses import asdict

    try:
        apply_channel_setup(
            channel="voice",
            config_dir=config_file.parent,
            selection_data=asdict(selection),
            force=bool(getattr(args, "force", False)),
        )
    except ChannelSetupError as exc:
        ui.print_panel(str(exc), title="Voice Setup Stopped", border_style="red")
        return 1

    _print_voice_post_setup(config_file, selection, agent_name=agent_name)
    return 0


def _feishu_channel_config(selection: FeishuInitSelection) -> dict[str, Any]:
    config: dict[str, Any] = {
        "app_id": selection.app_id,
        "app_secret": selection.app_secret,
        "stream": selection.stream,
        "group_fetch_limit": selection.group_fetch_limit,
        "group_reply_only_when_mentioned": selection.group_reply_only_when_mentioned,
    }
    return config


def collect_feishu_init_selection_terminal_ui(
    *,
    args: argparse.Namespace,
    ui: Optional[TerminalUI] = None,
    input_func: Optional[Callable[[str], str]] = None,
    secret_input_func: Optional[Callable[[str], str]] = None,
) -> Optional[FeishuInitSelection]:
    wizard_ui = ui or TerminalUI()
    interactive = wizard_ui.interactive
    input_func = input_func or input
    secret_input_func = secret_input_func or getpass.getpass

    app_id_arg = str(getattr(args, "app_id", "") or "").strip()
    app_secret_arg = str(getattr(args, "app_secret", "") or "").strip()
    manual_requested = bool(getattr(args, "manual", False) or app_id_arg or app_secret_arg)

    if manual_requested:
        credential_mode = "manual"
        if interactive:
            wizard_ui.record("App Access", "Use existing App ID / App Secret")
    elif interactive:
        choice = wizard_ui.select(
            label="App Access",
            subtitle="Choose how xAgent should get the Feishu credentials.",
            options=[
                MenuOption(
                    "one_click",
                    "Create new Feishu app",
                    "Recommended. Create a new Feishu app and authorize it.",
                ),
                MenuOption(
                    "manual",
                    "Use existing App ID / App Secret",
                    "Paste credentials from an app you already created in the Feishu developer console.",
                ),
                MenuOption(
                    "back",
                    "Back",
                    "Cancel Feishu setup and return.",
                ),
            ],
            default_index=0,
        )
        if choice is None or choice.key == "back":
            raise SetupCancelled()
        credential_mode = choice.key
    else:
        credential_mode = "one_click"

    if credential_mode == "one_click":
        credentials = _register_feishu_app_via_qr()
        if credentials is None:
            return None
        app_id, app_secret = credentials
        if interactive:
            wizard_ui.record("App ID", app_id)
    else:
        app_id = app_id_arg
        while not app_id:
            if interactive:
                app_id = wizard_ui.ask_text(
                    "Feishu App ID",
                    subtitle="Create or open the app in https://open.feishu.cn/app, then copy the App ID.",
                ).strip()
                if app_id:
                    break
                wizard_ui.print_panel("Feishu App ID is required.", title="Input Required")
                continue
            app_id = _prompt_text("Feishu App ID", input_func=input_func).strip()
            if not app_id:
                print("App ID is required.")
                return None
        if interactive and app_id_arg:
            wizard_ui.record("App ID", app_id)

        app_secret = app_secret_arg
        while not app_secret:
            if interactive:
                app_secret = wizard_ui.ask_secret("Feishu App Secret").strip()
                if app_secret:
                    break
                wizard_ui.print_panel("Feishu App Secret is required.", title="Input Required")
                continue
            app_secret = secret_input_func("Feishu App Secret: ").strip()
            if not app_secret:
                print("App Secret is required.")
                return None
        if interactive and app_secret_arg:
            wizard_ui.record("App Secret", "Provided via command line")

    group_reply_only_when_mentioned = bool(getattr(args, "group_reply_only_when_mentioned", False))

    stream_arg = getattr(args, "stream", None)
    stream = bool(stream_arg) if stream_arg is not None else False

    group_fetch_arg = getattr(args, "group_fetch_limit", None)
    if group_fetch_arg is not None and group_fetch_arg < 0:
        if interactive:
            wizard_ui.print_panel("--group-fetch-limit must be >= 0", title="Feishu Setup Stopped")
        else:
            print("--group-fetch-limit must be >= 0")
        return None
    group_fetch_limit = int(group_fetch_arg) if group_fetch_arg is not None else 10

    selection = FeishuInitSelection(
        app_id=app_id,
        app_secret=app_secret,
        stream=stream,
        group_fetch_limit=group_fetch_limit,
        group_reply_only_when_mentioned=group_reply_only_when_mentioned,
        credential_mode=credential_mode,
    )

    return selection


def _normalize_feishu_qr_payload(payload: Any) -> tuple[Optional[str], Optional[int], Optional[str]]:
    url: Optional[str] = None
    expire_in: Optional[int] = None
    if isinstance(payload, str):
        url = payload.strip() or None
    elif isinstance(payload, dict):
        raw_url = payload.get("url") or payload.get("verification_uri_complete")
        if raw_url is not None:
            url = str(raw_url).strip() or None
        raw_expire = payload.get("expire_in") or payload.get("expires_in")
        if raw_expire is not None:
            try:
                expire_in = int(raw_expire)
            except (TypeError, ValueError):
                expire_in = None
    elif payload is not None:
        url = str(payload).strip() or None

    user_code: Optional[str] = None
    if url:
        user_code = parse_qs(urlparse(url).query).get("user_code", [None])[0]
    return url, expire_in, user_code


def _format_feishu_expiry(expire_in: Optional[int]) -> Optional[str]:
    if expire_in is None or expire_in <= 0:
        return None
    minutes, seconds = divmod(expire_in, 60)
    if seconds == 0:
        unit = "minute" if minutes == 1 else "minutes"
        return f"{minutes} {unit}"
    if minutes:
        return f"{minutes}m {seconds}s"
    unit = "second" if seconds == 1 else "seconds"
    return f"{seconds} {unit}"


def _try_print_qr_ascii(url: str) -> bool:
    try:
        import qrcode
    except ImportError:
        return False

    try:
        qr = qrcode.QRCode()
        qr.add_data(url)
        qr.make()
        print("\n📱 Scan this QR code with your Feishu app:\n")
        qr.print_ascii(invert=True)
        return True
    except Exception:
        return False


def _print_feishu_post_setup(
    config_path: Path,
    selection: FeishuInitSelection,
    *,
    agent_name: str | None = None,
    show_next_steps: bool = True,
) -> None:
    config_dir = config_path.parent
    ui = TerminalUI()

    summary = Text()
    summary.append("Config:\n")
    summary.append(f"- App ID: {selection.app_id}\n")
    summary.append(f"- File: {config_path}\n")
    summary.append("- Manage app: https://open.feishu.cn/app/\n")
    summary.append("\n────────────────────────────────────────\n")
    summary.append("Optional: Enable group chat support\n\n")
    summary.append("Add permissions:\n")
    summary.append("- im:message.group_msg\n")
    summary.append("- im:message.group_at_msg.include_bot:readonly\n")
    summary.append("- contact:user.base:readonly\n")
    summary.append("- admin:app.info:readonly\n")
    summary.append("\nFor member names in groups, also set a Contact Scope for\n")
    summary.append("contact:user.base:readonly.\n")
    summary.append("────────────────────────────────────────")
    ui.print_panel(summary, title="Feishu Ready", leading_blank_line=True)

    if show_next_steps:
        feishu_start = _format_init_command("xagent feishu start", config_dir=config_dir, agent_name=agent_name)
        status = _format_init_command("xagent feishu status", config_dir=config_dir, agent_name=agent_name)
        logs = _format_init_command("xagent feishu logs -f", config_dir=config_dir, agent_name=agent_name)

        next_steps = Text()
        next_steps.append("Run next:\n")
        next_steps.append("start   ")
        next_steps.append(feishu_start, style="cyan")
        next_steps.append("\n        Start only the Feishu channel.\n")
        next_steps.append("status  ")
        next_steps.append(status, style="cyan")
        next_steps.append("\n        Check PID, logs, and whether the bot is already running.\n")
        next_steps.append("logs    ")
        next_steps.append(logs, style="cyan")
        next_steps.append("\n        Follow the Feishu channel log live.\n")

        ui.print_panel(next_steps, title="Next Steps")


def _register_feishu_app_via_qr(
    *,
    on_qr_update: Optional[Callable[[str, Optional[int]], None]] = None,
    on_status: Optional[Callable[[str], None]] = None,
    cancel_event: Optional[Any] = None,
    source: str = "xagent-cli",
) -> Optional[Tuple[str, str]]:
    try:
        from lark_oapi import register_app
        from lark_oapi.scene.registration import (
            AppAccessDeniedError,
            AppExpiredError,
            RegisterAppError,
        )
    except ImportError:
        message = "One-click registration requires lark-oapi>=1.5.5."
        if on_status is None:
            print(message)
            print("Upgrade with: pip install -U 'lark-oapi>=1.5.5'")
            print("Or rerun with --manual to enter the App ID/Secret yourself.")
        else:
            on_status("error")
        return None

    import threading

    local_cancel = cancel_event or threading.Event()

    def on_qr_code(qr_payload: Any) -> None:
        url, expire_in, user_code = _normalize_feishu_qr_payload(qr_payload)
        expiry_label = _format_feishu_expiry(expire_in)
        del user_code, expiry_label

        if on_qr_update is not None:
            if url:
                on_qr_update(url, expire_in)
            return

        if not url:
            print("\nFeishu returned an authorization step, but no browser link was included.")
            print("Please retry `xagent feishu setup`, or use `--manual` if the problem persists.")
            print("\nWaiting for authorization... (press Ctrl+C to cancel)\n")
            return

        print("\n🔗 Click this link to authorize (or paste into your browser):\n")
        print(f"{url}\n")

        if _try_print_qr_ascii(url):
            print("\n✓ Choose your preferred auth method above.")
        else:
            print("\n💡 Tip: Install qrcode for ASCII QR display: pip install qrcode[pil]")

        print("\nWaiting for authorization... (press Ctrl+C to cancel)\n")

    def on_status_change(info: dict) -> None:
        status = info.get("status")
        if status == "domain_switched":
            if on_status is not None:
                on_status("domain_switched")
            else:
                print("Switched to Lark Suite domain, continuing...")

    try:
        result = register_app(
            on_qr_code=on_qr_code,
            on_status_change=on_status_change,
            source=source,
            cancel_event=local_cancel,
        )
    except KeyboardInterrupt:
        local_cancel.set()
        if on_status is not None:
            on_status("cancelled")
        else:
            print("\nRegistration cancelled.")
        return None
    except AppAccessDeniedError:
        if on_status is not None:
            on_status("denied")
        else:
            print("\nAuthorization was denied. Ask a Feishu admin to approve the app, then retry.")
        return None
    except AppExpiredError:
        if on_status is not None:
            on_status("expired")
        else:
            print("\nThe authorization request expired. Rerun `xagent feishu setup` to try again.")
        return None
    except RegisterAppError as exc:
        error, description = (exc.args + ("", ""))[:2]
        if on_status is not None:
            on_status(f"error:{error} {description}".strip())
        else:
            print(f"\nRegistration failed: {error} {description}".rstrip())
        return None

    app_id = str(result.get("client_id") or "").strip()
    app_secret = str(result.get("client_secret") or "").strip()
    if not app_id or not app_secret:
        if on_status is not None:
            on_status("error:no_credentials")
        else:
            print("\nRegistration did not return credentials. Rerun with --manual to enter them yourself.")
        return None

    user_info = result.get("user_info") or {}
    user_name = user_info.get("name") or user_info.get("en_name")
    if on_status is None:
        if user_name:
            print(f"\nAuthorized by {user_name}.")
        print(f"Created Feishu app: {app_id}")
    return app_id, app_secret


def _ensure_api_port(channels_cfg: dict) -> None:
    """Set ``api.host`` / ``api.port`` in *channels_cfg* if they are missing.

    Port is only assigned when no port exists yet, reusing the
    :func:`allocate_api_port` heuristic.
    """
    api_cfg = channels_cfg.setdefault("api", {})
    if not isinstance(api_cfg, dict):
        return
    api_cfg.setdefault("host", BaseAgentConfig.DEFAULT_HOST)
    if "port" not in api_cfg:
        api_cfg["port"] = allocate_api_port()


def handle_init_feishu(args: argparse.Namespace) -> int:
    ui = TerminalUI()
    config_file = _config_path(args)
    agent_name = getattr(args, "agent", None)
    init_command = _format_init_command("xagent setup", config_dir=config_file.parent, agent_name=agent_name)
    if not config_file.is_file():
        ui.print_panel(
            f"Config not found: {config_file}\nRun {init_command} first, then return to Feishu setup.",
            title="Feishu Setup Stopped",
        )
        return 1

    try:
        with config_file.open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        ui.print_panel(f"Invalid YAML in {config_file}: {exc}", title="Feishu Setup Stopped", border_style="red")
        return 1
    if not isinstance(config, dict):
        ui.print_panel(f"Configuration must be a mapping: {config_file}", title="Feishu Setup Stopped", border_style="red")
        return 1

    channels_cfg = config.setdefault("channels", {})
    if not isinstance(channels_cfg, dict):
        ui.print_panel("channels must be a dictionary", title="Feishu Setup Stopped", border_style="red")
        return 1
    if "feishu" in channels_cfg and not args.force:
        force_command = _format_init_command(
            "xagent feishu setup --force",
            config_dir=config_file.parent,
            agent_name=agent_name,
        )
        ui.print_panel(
            f"channels.feishu already exists in {config_file}.\nRun {force_command} to overwrite the Feishu channel settings.",
            title="Feishu Setup Stopped",
        )
        return 1

    if getattr(args, "show_intro", True):
        _print_channel_setup_intro(
            ui,
            channel="feishu",
            config_file=config_file,
            replacing="feishu" in channels_cfg,
        )

    try:
        selection = collect_feishu_init_selection_terminal_ui(args=args, ui=ui)
    except SetupCancelled:
        return _setup_exit_on_cancel(ui, args, channel="feishu")
    except ReturnToLauncherHome:
        raise
    except KeyboardInterrupt:
        return _setup_exit_on_cancel(ui, args, channel="feishu")
    if selection is None:
        return 1

    from dataclasses import asdict

    try:
        apply_channel_setup(
            channel="feishu",
            config_dir=config_file.parent,
            selection_data=asdict(selection),
            force=bool(args.force),
        )
    except ChannelSetupError as exc:
        ui.print_panel(str(exc), title="Feishu Setup Stopped", border_style="red")
        return 1

    _print_feishu_post_setup(
        config_file,
        selection,
        agent_name=agent_name,
        show_next_steps=getattr(args, "show_next_steps", True),
    )
    return 0


def _normalize_repeated_values(values: Optional[Sequence[str]]) -> tuple[str, ...]:
    result: list[str] = []
    for raw_value in values or []:
        for item in str(raw_value).split(","):
            normalized = item.strip()
            if normalized and normalized not in result:
                result.append(normalized)
    return tuple(result)


def _try_print_weixin_qr_ascii(url: str) -> bool:
    try:
        import qrcode
    except ImportError:
        return False
    try:
        qr = qrcode.QRCode()
        qr.add_data(url)
        qr.make(fit=True)
        print("\nScan this QR code with WeChat:\n")
        qr.print_ascii(invert=True)
        return True
    except Exception:
        return False


def collect_weixin_init_selection_terminal_ui(
    *,
    args: argparse.Namespace,
    ui: Optional[TerminalUI] = None,
) -> Optional[WeixinInitSelection]:
    del ui
    from ...integrations.weixin.client import qr_login
    from ...integrations.weixin.config import ILINK_BASE_URL, WEIXIN_CDN_BASE_URL
    from ...integrations.weixin.state import WeixinStateStore

    config_dir = _runtime_dir(args)
    base_url = str(getattr(args, "base_url", None) or ILINK_BASE_URL).strip().rstrip("/")
    cdn_base_url = str(getattr(args, "cdn_base_url", None) or WEIXIN_CDN_BASE_URL).strip().rstrip("/")
    bot_type = str(getattr(args, "bot_type", None) or "3").strip() or "3"
    owner_only = bool(getattr(args, "owner_only", True))
    allow_users = _normalize_repeated_values(getattr(args, "allow_users", None))
    media_enabled = bool(getattr(args, "media_enabled", True))

    def log(message: str) -> None:
        print(message)

    def render_qr(url: str) -> None:
        print(url)
        if not _try_print_weixin_qr_ascii(url):
            print("Install qrcode[pil] for terminal QR rendering, or open the URL above.")

    try:
        credentials = asyncio.run(qr_login(
            base_url=base_url,
            bot_type=bot_type,
            log=log,
            render_qr_url=render_qr,
        ))
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        print(f"Weixin QR login failed: {exc}")
        return None

    store = WeixinStateStore(config_dir)
    store.save_credentials(credentials)
    return WeixinInitSelection(
        account_id=credentials.account_id,
        owner_user_id=credentials.user_id,
        base_url=credentials.base_url or base_url,
        cdn_base_url=cdn_base_url,
        owner_only=owner_only,
        allow_users=allow_users,
        media_enabled=media_enabled,
    )


def _weixin_channel_config(selection: WeixinInitSelection) -> dict[str, Any]:
    from ...integrations.weixin.config import weixin_channel_config_from_selection

    return weixin_channel_config_from_selection(
        account_id=selection.account_id,
        owner_user_id=selection.owner_user_id,
        base_url=selection.base_url,
        cdn_base_url=selection.cdn_base_url,
        owner_only=selection.owner_only,
        allow_users=list(selection.allow_users),
        media_enabled=selection.media_enabled,
    )


def _print_weixin_post_setup(
    config_path: Path,
    selection: WeixinInitSelection,
    *,
    agent_name: str | None = None,
    show_next_steps: bool = True,
) -> None:
    config_dir = config_path.parent
    ui = TerminalUI()
    summary = Text()
    summary.append(f"Weixin channel updated in {config_path}\n\n")
    summary.append("Configured behavior:\n")
    summary.append(f"- Account ID: {selection.account_id}\n")
    summary.append(f"- Owner User ID: {selection.owner_user_id}\n")
    summary.append(f"- Access: {_weixin_access_label(selection)}\n")
    summary.append(f"- Media: {'Enabled' if selection.media_enabled else 'Disabled'}\n")
    ui.print_panel(summary, title="Weixin Ready", leading_blank_line=True)

    if show_next_steps:
        start = _format_init_command("xagent weixin start", config_dir=config_dir, agent_name=agent_name)
        status = _format_init_command("xagent weixin status", config_dir=config_dir, agent_name=agent_name)
        logs = _format_init_command("xagent weixin logs -f", config_dir=config_dir, agent_name=agent_name)
        next_steps = Text()
        next_steps.append("Run next:\n")
        next_steps.append("start   ")
        next_steps.append(start, style="cyan")
        next_steps.append("\n        Start only the Weixin DM channel.\n")
        next_steps.append("status  ")
        next_steps.append(status, style="cyan")
        next_steps.append("\n        Check PID, logs, and whether the channel is running.\n")
        next_steps.append("logs    ")
        next_steps.append(logs, style="cyan")
        next_steps.append("\n        Follow the Weixin channel log live.\n")
        next_steps.append("\nOnly direct messages are supported. Group messages are ignored.")
        ui.print_panel(next_steps, title="Next Steps")


def handle_init_weixin(args: argparse.Namespace) -> int:
    ui = TerminalUI()
    config_file = _config_path(args)
    agent_name = getattr(args, "agent", None)
    init_command = _format_init_command("xagent setup", config_dir=config_file.parent, agent_name=agent_name)
    if not config_file.is_file():
        ui.print_panel(
            f"Config not found: {config_file}\nRun {init_command} first, then return to Weixin setup.",
            title="Weixin Setup Stopped",
        )
        return 1

    try:
        with config_file.open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        ui.print_panel(f"Invalid YAML in {config_file}: {exc}", title="Weixin Setup Stopped", border_style="red")
        return 1
    if not isinstance(config, dict):
        ui.print_panel(f"Configuration must be a mapping: {config_file}", title="Weixin Setup Stopped", border_style="red")
        return 1

    channels_cfg = config.setdefault("channels", {})
    if not isinstance(channels_cfg, dict):
        ui.print_panel("channels must be a dictionary", title="Weixin Setup Stopped", border_style="red")
        return 1
    if "weixin" in channels_cfg and not getattr(args, "force", False):
        force_command = _format_init_command(
            "xagent weixin setup --force",
            config_dir=config_file.parent,
            agent_name=agent_name,
        )
        ui.print_panel(
            f"channels.weixin already exists in {config_file}.\nRun {force_command} to refresh the Weixin login and overwrite settings.",
            title="Weixin Setup Stopped",
        )
        return 1

    if getattr(args, "show_intro", True):
        _print_channel_setup_intro(
            ui,
            channel="weixin",
            config_file=config_file,
            replacing="weixin" in channels_cfg,
            extra_lines=("This will open a Weixin iLink QR login.",),
        )
    try:
        selection = collect_weixin_init_selection_terminal_ui(args=args, ui=ui)
    except SetupCancelled:
        return _setup_exit_on_cancel(ui, args, channel="weixin")
    except ReturnToLauncherHome:
        raise
    except KeyboardInterrupt:
        return _setup_exit_on_cancel(ui, args, channel="weixin")
    if selection is None:
        return 1

    from dataclasses import asdict

    try:
        apply_channel_setup(
            channel="weixin",
            config_dir=config_file.parent,
            selection_data=asdict(selection),
            force=bool(getattr(args, "force", False)),
        )
    except ChannelSetupError as exc:
        ui.print_panel(str(exc), title="Weixin Setup Stopped", border_style="red")
        return 1

    _print_weixin_post_setup(
        config_file,
        selection,
        agent_name=agent_name,
        show_next_steps=getattr(args, "show_next_steps", True),
    )
    return 0
