"""Setup and initialization flows for the CLI."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, Tuple
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
    PROVIDER_MINIMAX,
    PROVIDER_OPENAI,
    PROVIDER_QWEN,
    model_api_uses_openai_client,
    normalize_provider_name,
    provider_base_url,
    provider_model_api,
)
from ...tools.search_tool import is_placeholder_api_key
from ..base import BaseAgentConfig
from .agents import allocate_api_port
from .paths import config_path as _config_path, runtime_dir as _runtime_dir, setup_runtime_dir as _setup_runtime_dir
from .terminal_ui import MenuOption, ReturnToLauncherHome, TerminalUI


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
    search_provider: str = "none"
    search_api_key: str = ""
    image_generation_provider: str = "none"
    image_generation_api_key: str = ""
    observability_enabled: bool = False
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str = ""
    voice_enabled: bool = False
    voice_provider: str = "none"
    voice_api_key: str = ""
    voice_stt_provider: str = ""
    voice_stt_api_key: str = ""
    voice_tts_provider: str = ""
    voice_tts_api_key: str = ""
    voice_enable_interruptions: bool = False
    voice_wake_enabled: bool = False
    voice_wake_phrases: Tuple[str, ...] = ()
    voice_exit_phrases: Tuple[str, ...] = ()


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


OPENAI_BASE_URL = provider_base_url(PROVIDER_OPENAI)
DEEPSEEK_BASE_URL = provider_base_url(PROVIDER_DEEPSEEK)
ANTHROPIC_BASE_URL = provider_base_url(PROVIDER_ANTHROPIC)
MINIMAX_BASE_URL = provider_base_url(PROVIDER_MINIMAX)
QWEN_BASE_URL = provider_base_url(PROVIDER_QWEN)
CUSTOM_OPENAI_BASE_URL_PLACEHOLDER = provider_base_url(PROVIDER_CUSTOM, MODEL_API_OPENAI_CHAT_COMPLETIONS)
CUSTOM_ANTHROPIC_BASE_URL_PLACEHOLDER = provider_base_url(PROVIDER_CUSTOM, MODEL_API_ANTHROPIC_MESSAGES)
API_KEY_PLACEHOLDER = "your_api_key_here"
OPENAI_SEARCH_API_KEY_PLACEHOLDER = "your_openai_api_key_here"
QWEN_SEARCH_API_KEY_PLACEHOLDER = "your_qwen_api_key_here"
OPENAI_IMAGE_API_KEY_PLACEHOLDER = "your_openai_api_key_here"
MINIMAX_IMAGE_API_KEY_PLACEHOLDER = "your_minimax_api_key_here"
QWEN_IMAGE_API_KEY_PLACEHOLDER = "your_qwen_api_key_here"
SONIOX_KEY_PLACEHOLDER = "your_soniox_api_key_here"
QWEN_KEY_PLACEHOLDER = "your_qwen_api_key_here"
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
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "gpt-5.5",
    "Decide later",
)
ANTHROPIC_MODELS = (
    "claude-sonnet-4-20250514",
    "claude-opus-4-1-20250805",
    "claude-3-5-haiku-20241022",
    "Decide later",
)
DEEPSEEK_MODELS = (
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "Decide later",
)
MINIMAX_MODELS = (
    "MiniMax-M3",
    "MiniMax-M2.7",
    "MiniMax-M2.7-highspeed",
)
QWEN_MODELS = (
    "qwen3.7-max",
    "qwen3.6-flash",
    "qwen3.6-plus",
    "Decide later",
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
VOICE_PROVIDERS = (
    "none",
    "soniox",
    "qwen",
    "custom",
)
VOICE_CUSTOM_PROVIDERS = (
    "soniox",
    "qwen",
)
DEFAULT_WAKE_PHRASES = ("xAgent",)
DEFAULT_EXIT_PHRASES = ("exit", "stop", "goodbye", "that's all", "never mind")


def _search_api_key_placeholder(provider: str) -> str:
    if provider == "openai":
        return OPENAI_SEARCH_API_KEY_PLACEHOLDER
    if provider == "qwen":
        return QWEN_SEARCH_API_KEY_PLACEHOLDER
    if provider == "minimax":
        return MINIMAX_IMAGE_API_KEY_PLACEHOLDER
    return API_KEY_PLACEHOLDER


def _image_generation_api_key_placeholder(provider: str) -> str:
    if provider == "openai":
        return OPENAI_IMAGE_API_KEY_PLACEHOLDER
    if provider == "minimax":
        return MINIMAX_IMAGE_API_KEY_PLACEHOLDER
    if provider == "qwen":
        return QWEN_IMAGE_API_KEY_PLACEHOLDER
    return API_KEY_PLACEHOLDER


def _voice_api_key_placeholder(provider: str) -> str:
    if provider == "qwen":
        return QWEN_KEY_PLACEHOLDER
    return SONIOX_KEY_PLACEHOLDER


def _feature_api_key_value(
    *,
    feature_provider: str,
    explicit_api_key: str,
    model_provider: str,
    model_api_key: str,
    placeholder: str,
) -> str:
    configured_key = explicit_api_key.strip()
    if configured_key:
        return configured_key

    if normalize_provider_name(feature_provider) == normalize_provider_name(model_provider):
        copied_key = model_api_key.strip()
        if copied_key and not is_placeholder_api_key(copied_key):
            return copied_key

    return placeholder


def _voice_defaults_for_provider(provider: str) -> dict[str, dict[str, str]]:
    if provider == "qwen":
        return {
            "stt": {
                "model": "qwen3-asr-flash-realtime",
            },
            "tts": {
                "model": "qwen3-tts-flash-realtime",
                "voice": "Cherry",
            },
        }
    return {
        "stt": {
            "model": "stt-rt-v4",
        },
        "tts": {
            "model": "tts-rt-v1",
            "voice": "Owen",
        },
    }


def _default_init_selection() -> InitSelection:
    return InitSelection(
        provider="openai",
        base_url=OPENAI_BASE_URL,
        api_key=API_KEY_PLACEHOLDER,
        model="gpt-5.4-mini",
        identity=_default_identity_markdown(),
        search_provider="none",
        image_generation_provider="none",
    )



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

    config = {
        "provider": provider_config,
        "agent": {
            "max_history": AgentConfig.DEFAULT_MAX_HISTORY,
            "max_iter": AgentConfig.DEFAULT_MAX_ITER,
            "max_concurrent_tools": AgentConfig.DEFAULT_MAX_CONCURRENT_TOOLS,
        },
        "channels": {
            "api": {
                "host": BaseAgentConfig.DEFAULT_HOST,
                "port": port,
            }
        },
    }
    if selection.voice_enabled:
        voice_provider = selection.voice_provider or "soniox"
        wake_phrases = list(selection.voice_wake_phrases) or list(DEFAULT_WAKE_PHRASES)
        exit_phrases = list(selection.voice_exit_phrases) or list(DEFAULT_EXIT_PHRASES)
        voice_config = {
            "provider": voice_provider,
            "enable_interruptions": selection.voice_enable_interruptions,
            "audio": {
                "input": "auto",
                "output": "auto",
            },
            "wake": {
                "enabled": selection.voice_wake_enabled,
                "wake_phrases": wake_phrases,
                "exit_phrases": exit_phrases,
                "match_mode": "prefix",
                "idle_timeout_seconds": 60,
            },
        }
        if voice_provider == "custom":
            stt_provider = selection.voice_stt_provider or "soniox"
            tts_provider = selection.voice_tts_provider or "qwen"
            stt_defaults = _voice_defaults_for_provider(stt_provider)["stt"]
            tts_defaults = _voice_defaults_for_provider(tts_provider)["tts"]
            voice_config["stt"] = {
                "provider": stt_provider,
                "api_key": selection.voice_stt_api_key.strip() or _voice_api_key_placeholder(stt_provider),
                **stt_defaults,
            }
            voice_config["tts"] = {
                "provider": tts_provider,
                "api_key": selection.voice_tts_api_key.strip() or _voice_api_key_placeholder(tts_provider),
                **tts_defaults,
            }
        else:
            voice_api_key = selection.voice_api_key.strip() or _voice_api_key_placeholder(voice_provider)
            voice_defaults = _voice_defaults_for_provider(voice_provider)
            voice_config["stt"] = {
                "api_key": voice_api_key,
                **voice_defaults["stt"],
            }
            voice_config["tts"] = {
                "api_key": voice_api_key,
                **voice_defaults["tts"],
            }
        config["channels"]["voice"] = voice_config
    search_config = {"provider": selection.search_provider or "none"}
    if search_config["provider"] in {"openai", "qwen", "minimax"}:
        search_config["api_key"] = _feature_api_key_value(
            feature_provider=search_config["provider"],
            explicit_api_key=selection.search_api_key,
            model_provider=selection.provider,
            model_api_key=selection.api_key,
            placeholder=_search_api_key_placeholder(search_config["provider"]),
        )
    config["search"] = search_config
    selected_image_generation_provider = selection.image_generation_provider or "none"
    image_generation_config = {"provider": selected_image_generation_provider}
    if selected_image_generation_provider in {"openai", "minimax", "qwen"}:
        image_generation_config["api_key"] = _feature_api_key_value(
            feature_provider=selected_image_generation_provider,
            explicit_api_key=selection.image_generation_api_key,
            model_provider=selection.provider,
            model_api_key=selection.api_key,
            placeholder=_image_generation_api_key_placeholder(selected_image_generation_provider),
        )
    config["image_generation"] = image_generation_config
    if selection.observability_enabled:
        config["observability"] = {
            "enabled": True,
            "provider": "langfuse",
            "public_key": selection.langfuse_public_key or LANGFUSE_PUBLIC_KEY_PLACEHOLDER,
            "secret_key": selection.langfuse_secret_key or LANGFUSE_SECRET_KEY_PLACEHOLDER,
            "base_url": selection.langfuse_base_url or LANGFUSE_BASE_URL,
        }
    return yaml.safe_dump(config, sort_keys=False, allow_unicode=False)


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


def _select_search_provider(
    provider: str,
    *,
    input_func: Callable[[str], str] = input,
) -> str:
    del provider
    return _select_option(
        "Search provider",
        SEARCH_PROVIDERS,
        default_index=0,
        input_func=input_func,
    )


def _select_image_generation_provider(
    provider: str,
    *,
    input_func: Callable[[str], str] = input,
) -> str:
    del provider
    return _select_option(
        "Image generation provider",
        IMAGE_GENERATION_PROVIDERS,
        default_index=0,
        input_func=input_func,
    )


def _prompt_image_generation_api_key(
    image_generation_provider: str,
    *,
    secret_input_func: Callable[[str], str] = getpass.getpass,
) -> str:
    if image_generation_provider == "openai":
        api_key = secret_input_func(
            "OpenAI API key for image generation (leave blank to fill in later): "
        ).strip()
    elif image_generation_provider == "minimax":
        api_key = secret_input_func(
            "MiniMax API key for image generation (leave blank to fill in later): "
        ).strip()
    elif image_generation_provider == "qwen":
        api_key = secret_input_func(
            "Qwen API key for image generation (leave blank to fill in later): "
        ).strip()
    else:
        return ""
    return api_key or _image_generation_api_key_placeholder(image_generation_provider)


def _prompt_search_api_key(
    search_provider: str,
    *,
    secret_input_func: Callable[[str], str] = getpass.getpass,
) -> str:
    prompt_names = {
        "openai": "OpenAI",
        "qwen": "Qwen",
        "minimax": "MiniMax",
    }
    prompt_name = prompt_names.get(search_provider)
    if not prompt_name:
        return ""
    api_key = secret_input_func(
        f"{prompt_name} API key for search (leave blank to fill in later): "
    ).strip()
    return api_key or _search_api_key_placeholder(search_provider)


def _prompt_voice_api_key(
    voice_provider: str,
    *,
    provider: str,
    main_api_key: str,
    purpose: str = "voice",
    secret_input_func: Callable[[str], str] = getpass.getpass,
) -> str:
    if voice_provider == "qwen" and provider == PROVIDER_QWEN:
        return main_api_key if main_api_key != API_KEY_PLACEHOLDER else QWEN_KEY_PLACEHOLDER

    prompt_name = "Qwen" if voice_provider == "qwen" else "Soniox"
    api_key = secret_input_func(
        f"{prompt_name} API key for {purpose} (leave blank to fill in later): "
    ).strip()
    return api_key or _voice_api_key_placeholder(voice_provider)


def _phrase_prompt_default(values: Sequence[str]) -> str:
    return ", ".join(str(value).strip() for value in values if str(value).strip())


def _collect_voice_startup_preferences(
    *,
    prompt_yes_no: Callable[[str], bool],
    prompt_text: Callable[[str, str], str],
) -> tuple[bool, tuple[str, ...], tuple[str, ...], bool]:
    wake_enabled = prompt_yes_no("Enable wake phrases for voice?")
    wake_phrases: tuple[str, ...] = ()
    exit_phrases: tuple[str, ...] = ()
    if wake_enabled:
        wake_phrases = tuple(
            _phrase_list(prompt_text("Wake Phrases", _phrase_prompt_default(DEFAULT_WAKE_PHRASES)))
            or DEFAULT_WAKE_PHRASES
        )
        exit_phrases = tuple(
            _phrase_list(prompt_text("Exit Phrases", _phrase_prompt_default(DEFAULT_EXIT_PHRASES)))
            or DEFAULT_EXIT_PHRASES
        )
    interruptions_enabled = prompt_yes_no("Enable voice interruptions?")
    return wake_enabled, wake_phrases, exit_phrases, interruptions_enabled


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


def collect_init_selection_terminal_ui(
    *,
    ui: Optional[TerminalUI] = None,
    secret_input_func: Callable[[str], str] = getpass.getpass,
) -> InitSelection:
    wizard_ui = ui or TerminalUI()
    ask_secret = wizard_ui.ask_secret if wizard_ui.interactive else secret_input_func

    provider = _terminal_select_option(
        wizard_ui,
        "Provider",
        KNOWN_PROVIDERS,
        descriptions={
            PROVIDER_OPENAI: "GPT family via the OpenAI platform.",
            PROVIDER_DEEPSEEK: "DeepSeek chat and coding models.",
            PROVIDER_MINIMAX: "MiniMax models via the Anthropic-style API.",
            PROVIDER_QWEN: "Qwen models via DashScope-compatible APIs.",
            PROVIDER_ANTHROPIC: "Claude models via Anthropic Messages.",
            PROVIDER_CUSTOM: "Bring your own OpenAI, Responses, or Anthropic endpoint.",
        },
        subtitle="Choose the model provider to configure.",
    )
    model_api = ""
    supports_vision = False

    if provider == PROVIDER_OPENAI:
        selected_model = _terminal_select_model_option(
            wizard_ui,
            "OpenAI Model",
            OPENAI_MODELS,
            descriptions={
                "gpt-5.4": "Highest capability general model.",
                "gpt-5.4-mini": "Balanced default for speed and quality.",
                "gpt-5.4-nano": "Lowest latency and cost.",
                "gpt-5.5": "Newest OpenAI release.",
                "Decide later": "Write a placeholder and fill it in later.",
            },
            default_index=1,
        )
        base_url = OPENAI_BASE_URL
    elif provider == PROVIDER_ANTHROPIC:
        selected_model = _terminal_select_model_option(
            wizard_ui,
            "Anthropic Model",
            ANTHROPIC_MODELS,
            default_index=0,
        )
        base_url = ANTHROPIC_BASE_URL
    elif provider == PROVIDER_DEEPSEEK:
        selected_model = _terminal_select_model_option(
            wizard_ui,
            "DeepSeek Model",
            DEEPSEEK_MODELS,
            default_index=0,
        )
        base_url = DEEPSEEK_BASE_URL
    elif provider == PROVIDER_MINIMAX:
        selected_model = _terminal_select_model_option(
            wizard_ui,
            "MiniMax Model",
            MINIMAX_MODELS,
            default_index=0,
        )
        base_url = MINIMAX_BASE_URL
    elif provider == PROVIDER_QWEN:
        selected_model = _terminal_select_model_option(
            wizard_ui,
            "Qwen Model",
            QWEN_MODELS,
            default_index=1,
        )
        base_url = QWEN_BASE_URL
    else:
        model_api = _terminal_select_option(
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
        selected_model = "Decide later"
        default_base_url = (
            CUSTOM_ANTHROPIC_BASE_URL_PLACEHOLDER
            if model_api == MODEL_API_ANTHROPIC_MESSAGES
            else CUSTOM_OPENAI_BASE_URL_PLACEHOLDER
        )
        base_url = _terminal_prompt_text(
            wizard_ui,
            "Custom provider base URL",
            default=default_base_url,
        )
        supports_vision = _terminal_prompt_yes_no(
            wizard_ui,
            "Does this custom provider support image URL input?",
            default=False,
        )

    model = MODEL_PLACEHOLDER if selected_model == "Decide later" else selected_model
    api_key = ask_secret("API key (leave blank to fill in later): ").strip() or API_KEY_PLACEHOLDER

    provider_api_cfg = {"name": provider}
    if model_api:
        provider_api_cfg["model_api"] = model_api
    selected_model_api = provider_model_api(provider_api_cfg)

    observability_enabled = False
    langfuse_public_key = ""
    langfuse_secret_key = ""
    langfuse_base_url = ""
    if model_api_uses_openai_client(selected_model_api):
        observability_enabled = _terminal_prompt_yes_no(
            wizard_ui,
            "Enable Langfuse observability?",
            default=False,
        )
    if observability_enabled:
        langfuse_public_key = _terminal_prompt_text(
            wizard_ui,
            "Langfuse public key",
            default=LANGFUSE_PUBLIC_KEY_PLACEHOLDER,
        )
        langfuse_secret_key = (
            ask_secret("Langfuse secret key (leave blank to fill in later): ").strip()
            or LANGFUSE_SECRET_KEY_PLACEHOLDER
        )
        langfuse_base_url = _terminal_prompt_text(
            wizard_ui,
            "Langfuse base URL",
            default=LANGFUSE_BASE_URL,
        )

    search_provider = _terminal_select_option(
        wizard_ui,
        "Search Provider",
        SEARCH_PROVIDERS,
        descriptions={
            "none": "Do not enable a provider-native web search tool.",
            "openai": "Use OpenAI web search.",
            "qwen": "Use Qwen web search via DashScope.",
            "minimax": "Use MiniMax web search.",
        },
        default_index=0,
    )
    search_api_key = ""
    if search_provider in {"openai", "qwen", "minimax"} and search_provider != provider:
        search_api_key = _prompt_search_api_key(search_provider, secret_input_func=ask_secret)

    image_generation_provider = _terminal_select_option(
        wizard_ui,
        "Image Generation Provider",
        IMAGE_GENERATION_PROVIDERS,
        descriptions={
            "none": "Do not enable image generation.",
            "openai": "Use OpenAI image generation.",
            "minimax": "Use MiniMax image generation.",
            "qwen": "Use Qwen image generation via DashScope.",
        },
        default_index=0,
    )
    image_generation_api_key = ""
    if image_generation_provider in {"openai", "minimax", "qwen"} and image_generation_provider != provider:
        image_generation_api_key = _prompt_image_generation_api_key(
            image_generation_provider,
            secret_input_func=ask_secret,
        )

    voice_enabled = _terminal_prompt_yes_no(wizard_ui, "Enable voice mode?", default=False)
    voice_provider = "none"
    voice_api_key = ""
    voice_stt_provider = ""
    voice_stt_api_key = ""
    voice_tts_provider = ""
    voice_tts_api_key = ""
    voice_enable_interruptions = False
    voice_wake_enabled = False
    voice_wake_phrases: tuple[str, ...] = ()
    voice_exit_phrases: tuple[str, ...] = ()

    if voice_enabled:
        voice_provider = _terminal_select_option(
            wizard_ui,
            "Voice Provider",
            VOICE_PROVIDERS,
            descriptions={
                "none": "Disable voice features.",
                "soniox": "Use Soniox voice runtime defaults.",
                "qwen": "Use Qwen voice runtime defaults.",
                "custom": "Pick separate STT and TTS providers.",
            },
            default_index=1,
        )
        voice_enabled = voice_provider != "none"

    if voice_enabled:
        if voice_provider == "custom":
            voice_stt_provider = _terminal_select_option(
                wizard_ui,
                "STT Provider",
                VOICE_CUSTOM_PROVIDERS,
                default_index=0,
            )
            voice_stt_api_key = _prompt_voice_api_key(
                voice_stt_provider,
                provider=provider,
                main_api_key=api_key,
                purpose="STT",
                secret_input_func=ask_secret,
            )
            voice_tts_provider = _terminal_select_option(
                wizard_ui,
                "TTS Provider",
                VOICE_CUSTOM_PROVIDERS,
                default_index=0,
            )
            voice_tts_api_key = _prompt_voice_api_key(
                voice_tts_provider,
                provider=provider,
                main_api_key=api_key,
                purpose="TTS",
                secret_input_func=ask_secret,
            )
        elif voice_provider == "qwen" and provider == PROVIDER_QWEN:
            voice_api_key = api_key if api_key != API_KEY_PLACEHOLDER else QWEN_KEY_PLACEHOLDER
        else:
            voice_api_key = _prompt_voice_api_key(
                voice_provider,
                provider=provider,
                main_api_key=api_key,
                secret_input_func=ask_secret,
            )
        (
            voice_wake_enabled,
            voice_wake_phrases,
            voice_exit_phrases,
            voice_enable_interruptions,
        ) = _collect_voice_startup_preferences(
            prompt_yes_no=lambda prompt: _terminal_prompt_yes_no(wizard_ui, prompt, default=False),
            prompt_text=lambda prompt, default: wizard_ui.ask_text(
                prompt,
                default=default,
                subtitle="Separate phrases with commas.",
            ),
        )

    identity = _terminal_prompt_multiline_identity(wizard_ui)

    return InitSelection(
        provider=provider,
        model_api=model_api,
        supports_vision=supports_vision,
        base_url=base_url,
        api_key=api_key,
        model=model,
        identity=identity,
        search_provider=search_provider,
        search_api_key=search_api_key,
        image_generation_provider=image_generation_provider,
        image_generation_api_key=image_generation_api_key,
        observability_enabled=observability_enabled,
        langfuse_public_key=langfuse_public_key,
        langfuse_secret_key=langfuse_secret_key,
        langfuse_base_url=langfuse_base_url,
        voice_enabled=voice_enabled,
        voice_provider=voice_provider,
        voice_api_key=voice_api_key,
        voice_stt_provider=voice_stt_provider,
        voice_stt_api_key=voice_stt_api_key,
        voice_tts_provider=voice_tts_provider,
        voice_tts_api_key=voice_tts_api_key,
        voice_enable_interruptions=voice_enable_interruptions,
        voice_wake_enabled=voice_wake_enabled,
        voice_wake_phrases=voice_wake_phrases,
        voice_exit_phrases=voice_exit_phrases,
    )


def collect_init_selection(
    *,
    input_func: Callable[[str], str] = input,
    secret_input_func: Callable[[str], str] = getpass.getpass,
) -> InitSelection:
    print("\nxAgent init")
    print("Configure the runtime first; files will be written after these choices.")

    provider = _select_option(
        "Provider",
        KNOWN_PROVIDERS,
        input_func=input_func,
    )
    model_api = ""
    supports_vision = False

    if provider == PROVIDER_OPENAI:
        selected_model = _select_model_option(
            "OpenAI model",
            OPENAI_MODELS,
            default_index=1,
            input_func=input_func,
        )
        base_url = OPENAI_BASE_URL
    elif provider == PROVIDER_ANTHROPIC:
        selected_model = _select_model_option(
            "Anthropic model",
            ANTHROPIC_MODELS,
            default_index=0,
            input_func=input_func,
        )
        base_url = ANTHROPIC_BASE_URL
    elif provider == PROVIDER_DEEPSEEK:
        selected_model = _select_model_option(
            "DeepSeek model",
            DEEPSEEK_MODELS,
            default_index=0,
            input_func=input_func,
        )
        base_url = DEEPSEEK_BASE_URL
    elif provider == PROVIDER_MINIMAX:
        selected_model = _select_model_option(
            "MiniMax model",
            MINIMAX_MODELS,
            default_index=0,
            input_func=input_func,
        )
        base_url = MINIMAX_BASE_URL
    elif provider == PROVIDER_QWEN:
        selected_model = _select_model_option(
            "Qwen model",
            QWEN_MODELS,
            default_index=1,
            input_func=input_func,
        )
        base_url = QWEN_BASE_URL
    else:
        model_api = _select_custom_model_api(input_func=input_func)
        selected_model = "Decide later"
        default_base_url = (
            CUSTOM_ANTHROPIC_BASE_URL_PLACEHOLDER
            if model_api == MODEL_API_ANTHROPIC_MESSAGES
            else CUSTOM_OPENAI_BASE_URL_PLACEHOLDER
        )
        base_url = _prompt_text(
            "Custom provider base URL",
            default=default_base_url,
            input_func=input_func,
        )
        supports_vision = _prompt_yes_no(
            "Does this custom provider support image URL input?",
            default=False,
            input_func=input_func,
        )

    model = MODEL_PLACEHOLDER if selected_model == "Decide later" else selected_model
    api_key = secret_input_func("API key (leave blank to fill in later): ").strip()
    if not api_key:
        api_key = API_KEY_PLACEHOLDER

    provider_api_cfg = {"name": provider}
    if model_api:
        provider_api_cfg["model_api"] = model_api
    selected_model_api = provider_model_api(provider_api_cfg)
    observability_enabled = False
    langfuse_public_key = ""
    langfuse_secret_key = ""
    langfuse_base_url = ""
    if model_api_uses_openai_client(selected_model_api):
        observability_enabled = _prompt_yes_no(
            "Enable Langfuse observability?",
            default=False,
            input_func=input_func,
        )
    if observability_enabled:
        langfuse_public_key = _prompt_text(
            "Langfuse public key",
            default=LANGFUSE_PUBLIC_KEY_PLACEHOLDER,
            input_func=input_func,
        )
        langfuse_secret_key = secret_input_func(
            "Langfuse secret key (leave blank to fill in later): "
        ).strip()
        if not langfuse_secret_key:
            langfuse_secret_key = LANGFUSE_SECRET_KEY_PLACEHOLDER
        langfuse_base_url = _prompt_text(
            "Langfuse base URL",
            default=LANGFUSE_BASE_URL,
            input_func=input_func,
        )

    search_provider = _select_search_provider(provider, input_func=input_func)
    search_api_key = ""
    if search_provider in {"openai", "qwen", "minimax"} and search_provider != provider:
        search_api_key = _prompt_search_api_key(
            search_provider,
            secret_input_func=secret_input_func,
        )

    image_generation_provider = _select_image_generation_provider(provider, input_func=input_func)
    image_generation_api_key = ""
    if image_generation_provider == "openai" and provider != PROVIDER_OPENAI:
        image_generation_api_key = _prompt_image_generation_api_key(
            image_generation_provider,
            secret_input_func=secret_input_func,
        )
    elif image_generation_provider == "minimax" and provider != PROVIDER_MINIMAX:
        image_generation_api_key = _prompt_image_generation_api_key(
            image_generation_provider,
            secret_input_func=secret_input_func,
        )
    elif image_generation_provider == "qwen" and provider != PROVIDER_QWEN:
        image_generation_api_key = _prompt_image_generation_api_key(
            image_generation_provider,
            secret_input_func=secret_input_func,
        )

    voice_enabled = False
    voice_provider = "none"
    voice_api_key = ""
    voice_stt_provider = ""
    voice_stt_api_key = ""
    voice_tts_provider = ""
    voice_tts_api_key = ""
    voice_enable_interruptions = False
    voice_wake_enabled = False
    voice_wake_phrases: tuple[str, ...] = ()
    voice_exit_phrases: tuple[str, ...] = ()

    voice_enabled = _prompt_yes_no(
        "Enable voice mode?",
        default=False,
        input_func=input_func,
    )
    if voice_enabled:
        voice_provider = _select_option(
            "Voice provider",
            VOICE_PROVIDERS,
            default_index=1,
            input_func=input_func,
        )
        voice_enabled = voice_provider != "none"
    if voice_enabled:
        if voice_provider == "custom":
            voice_stt_provider = _select_option(
                "STT provider",
                VOICE_CUSTOM_PROVIDERS,
                default_index=0,
                input_func=input_func,
            )
            voice_stt_api_key = _prompt_voice_api_key(
                voice_stt_provider,
                provider=provider,
                main_api_key=api_key,
                purpose="STT",
                secret_input_func=secret_input_func,
            )
            voice_tts_provider = _select_option(
                "TTS provider",
                VOICE_CUSTOM_PROVIDERS,
                default_index=0,
                input_func=input_func,
            )
            voice_tts_api_key = _prompt_voice_api_key(
                voice_tts_provider,
                provider=provider,
                main_api_key=api_key,
                purpose="TTS",
                secret_input_func=secret_input_func,
            )
        elif voice_provider == "qwen" and provider == PROVIDER_QWEN:
            voice_api_key = api_key if api_key != API_KEY_PLACEHOLDER else QWEN_KEY_PLACEHOLDER
        else:
            voice_api_key = _prompt_voice_api_key(
                voice_provider,
                provider=provider,
                main_api_key=api_key,
                secret_input_func=secret_input_func,
            )
        (
            voice_wake_enabled,
            voice_wake_phrases,
            voice_exit_phrases,
            voice_enable_interruptions,
        ) = _collect_voice_startup_preferences(
            prompt_yes_no=lambda prompt: _prompt_yes_no(prompt, default=False, input_func=input_func),
            prompt_text=lambda prompt, default: _prompt_text(prompt, default=default, input_func=input_func),
        )

    identity = _prompt_multiline_identity(input_func=input_func)

    return InitSelection(
        provider=provider,
        model_api=model_api,
        supports_vision=supports_vision,
        base_url=base_url,
        api_key=api_key,
        model=model,
        identity=identity,
        search_provider=search_provider,
        search_api_key=search_api_key,
        image_generation_provider=image_generation_provider,
        image_generation_api_key=image_generation_api_key,
        observability_enabled=observability_enabled,
        langfuse_public_key=langfuse_public_key,
        langfuse_secret_key=langfuse_secret_key,
        langfuse_base_url=langfuse_base_url,
        voice_enabled=voice_enabled,
        voice_provider=voice_provider,
        voice_api_key=voice_api_key,
        voice_stt_provider=voice_stt_provider,
        voice_stt_api_key=voice_stt_api_key,
        voice_tts_provider=voice_tts_provider,
        voice_tts_api_key=voice_tts_api_key,
        voice_enable_interruptions=voice_enable_interruptions,
        voice_wake_enabled=voice_wake_enabled,
        voice_wake_phrases=voice_wake_phrases,
        voice_exit_phrases=voice_exit_phrases,
    )


def init_agent_directory(
    config_dir: Optional[str] = None,
    *,
    force: bool = False,
    selection: Optional[InitSelection] = None,
    clear_runtime_data: bool = False,
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
    port = allocate_api_port()
    config_file.write_text(_config_yaml(selection, port=port), encoding="utf-8")
    identity_file.write_text(selection.identity, encoding="utf-8")

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


def _print_init_next_steps(*, config_dir: Path, selection: InitSelection, agent_name: str | None = None) -> None:
    ready_now = [
        (
            "chat",
            _format_init_command("xagent chat", config_dir=config_dir, agent_name=agent_name),
            "Talk to the agent in your terminal.",
        ),
        (
            "web",
            _format_init_command("xagent web", config_dir=config_dir, agent_name=agent_name),
            "Open the built-in Web UI.",
        ),
        (
            "api",
            _format_init_command("xagent api start", config_dir=config_dir, agent_name=agent_name),
            "Run the HTTP / SSE / WebSocket channel in the background.",
        ),
    ]
    if selection.voice_enabled:
        ready_now.insert(
            2,
            (
                "voice",
                _format_init_command("xagent voice start", config_dir=config_dir, agent_name=agent_name),
                "Run the microphone/speaker channel in the background.",
            ),
        )

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


def _feishu_routing_label(group_reply_only_when_mentioned: bool) -> str:
    if group_reply_only_when_mentioned:
        return "Direct chats + group/topic @mentions only"
    return "Direct chats + group presence, agent self-decides when to speak"


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
            selection=selection,
            agent_name=getattr(args, "agent", None),
        )
    return 0 if result.wrote_files else 1


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
        choice = wizard_ui.select_menu(
            title="App Access",
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
            footer="↑/↓ Move • Enter Select  •  q Back",
        )
        if choice is None or choice.key == "back":
            raise KeyboardInterrupt()
        credential_mode = choice.key
        wizard_ui.record("App Access", choice.title)
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

    reply_only_arg = getattr(args, "group_reply_only_when_mentioned", None)
    if reply_only_arg is not None:
        group_reply_only_when_mentioned = bool(reply_only_arg)
    else:
        group_reply_only_when_mentioned = False
    if interactive:
        wizard_ui.record("Group Routing", _feishu_routing_label(group_reply_only_when_mentioned))

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
) -> None:
    config_dir = config_path.parent
    ui = TerminalUI()

    summary = Text()
    summary.append(f"Feishu channel updated in {config_path}\n\n")
    summary.append("Configured behavior:\n")
    summary.append(f"- App ID: {selection.app_id}\n")
    summary.append(f"- Routing: {_feishu_routing_label(selection.group_reply_only_when_mentioned)}\n")
    ui.print_panel(summary, title="Feishu Ready", leading_blank_line=True)

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

    next_steps.append("\nOptional before group rollout:\n")
    next_steps.append("- im:message.group_msg\n")
    next_steps.append("- im:message.group_at_msg.include_bot:readonly\n")
    next_steps.append("- contact:user.base:readonly\n")
    next_steps.append("- admin:app.info:readonly\n")
    next_steps.append("\nIf you only need direct chats right now, you can skip the group permission work and start the bot immediately.")
    if selection.group_reply_only_when_mentioned:
        next_steps.append("\n\n")
        next_steps.append(
            "Conservative group mode is enabled: unmentioned group messages are recorded for memory but will not be answered.",
            style="yellow",
        )

    ui.print_panel(next_steps, title="Next Steps")


def _register_feishu_app_via_qr() -> Optional[Tuple[str, str]]:
    try:
        from lark_oapi import register_app
        from lark_oapi.scene.registration import (
            AppAccessDeniedError,
            AppExpiredError,
            RegisterAppError,
        )
    except ImportError:
        print("One-click registration requires lark-oapi>=1.5.5.")
        print("Upgrade with: pip install -U 'lark-oapi>=1.5.5'")
        print("Or rerun with --manual to enter the App ID/Secret yourself.")
        return None

    import threading

    cancel_event = threading.Event()

    def on_qr_code(qr_payload: Any) -> None:
        url, expire_in, user_code = _normalize_feishu_qr_payload(qr_payload)
        expiry_label = _format_feishu_expiry(expire_in)
        del user_code, expiry_label

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
            print("Switched to Lark Suite domain, continuing...")

    try:
        result = register_app(
            on_qr_code=on_qr_code,
            on_status_change=on_status_change,
            source="xagent-cli",
            cancel_event=cancel_event,
        )
    except KeyboardInterrupt:
        cancel_event.set()
        print("\nRegistration cancelled.")
        return None
    except AppAccessDeniedError:
        print("\nAuthorization was denied. Ask a Feishu admin to approve the app, then retry.")
        return None
    except AppExpiredError:
        print("\nThe authorization request expired. Rerun `xagent feishu setup` to try again.")
        return None
    except RegisterAppError as exc:
        error, description = (exc.args + ("", ""))[:2]
        print(f"\nRegistration failed: {error} {description}".rstrip())
        return None

    app_id = str(result.get("client_id") or "").strip()
    app_secret = str(result.get("client_secret") or "").strip()
    if not app_id or not app_secret:
        print("\nRegistration did not return credentials. Rerun with --manual to enter them yourself.")
        return None

    user_info = result.get("user_info") or {}
    user_name = user_info.get("name") or user_info.get("en_name")
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

    intro_lines = [
        f"Runtime: {config_file.parent}",
        f"Config: {config_file}",
    ]
    if "feishu" in channels_cfg:
        intro_lines.append("Existing channels.feishu settings will be replaced.")
    ui.print_panel("\n".join(intro_lines), title="Feishu Setup", leading_blank_line=True)

    try:
        selection = collect_feishu_init_selection_terminal_ui(args=args, ui=ui)
    except (KeyboardInterrupt, ReturnToLauncherHome):
        ui.print_panel("Feishu setup cancelled before writing config.", title="Feishu Setup Cancelled")
        return 1
    if selection is None:
        return 1

    _ensure_api_port(channels_cfg)
    channels_cfg["feishu"] = _feishu_channel_config(selection)

    config_file.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=False), encoding="utf-8")
    _print_feishu_post_setup(config_file, selection, agent_name=agent_name)
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

    ui.print_panel(
        f"Runtime: {config_file.parent}\nConfig: {config_file}\nThis will open a Weixin iLink QR login.",
        title="Weixin Setup",
        leading_blank_line=True,
    )
    try:
        selection = collect_weixin_init_selection_terminal_ui(args=args, ui=ui)
    except KeyboardInterrupt:
        ui.print_panel("Weixin setup cancelled before writing config.", title="Weixin Setup Cancelled")
        return 1
    if selection is None:
        return 1

    _ensure_api_port(channels_cfg)
    channels_cfg["weixin"] = _weixin_channel_config(selection)
    config_file.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=False), encoding="utf-8")
    _print_weixin_post_setup(config_file, selection, agent_name=agent_name)
    return 0


def _phrase_list(raw_value: str) -> list[str]:
    return [item.strip() for item in raw_value.split(",") if item.strip()]
