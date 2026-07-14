"""Interactive launcher and setup editing flows for the CLI."""

from __future__ import annotations

import webbrowser
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import yaml
from rich.text import Text  # type: ignore[import-not-found]

from ...core.providers import (
    KNOWN_PROVIDERS,
    MODEL_API_OPENAI_CHAT_COMPLETIONS,
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
from .agents import (
    AgentRegistryError,
    default_agent_dir,
    handle_agents,
    load_agent_registry,
    management_root,
    select_agent,
)
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
from .web_client import web_client_config, web_client_paths
from .runtime import (
    _launcher_args,
    _xagent_version_text,
    handle_chat,
    handle_web_logs,
    handle_web_restart,
    handle_web_start,
    handle_web_status,
    handle_web_stop,
    handle_web_open,
    handle_config,
    handle_identity,
    handle_messages,
    handle_restart,
    handle_start,
    handle_status,
    handle_stop,
    handle_voice,
)
from .processes import managed_paths, running_pid
from .setup import (
    ANTHROPIC_MODELS,
    CUSTOM_MODEL_OPTION,
    DEFAULT_MEMORY_LIST_DAYS,
    DEFAULT_MESSAGE_LIST_COUNT,
    DEEPSEEK_MODELS,
    IMAGE_GENERATION_PROVIDERS,
    LANGFUSE_BASE_URL,
    LANGFUSE_PUBLIC_KEY_PLACEHOLDER,
    LANGFUSE_SECRET_KEY_PLACEHOLDER,
    MEMORY_LIST_DAY_CHOICES,
    MESSAGE_LIST_COUNT_CHOICES,
    MINIMAX_MODELS,
    MODEL_PLACEHOLDER,
    OPENAI_MODELS,
    QWEN_MODELS,
    SEARCH_PROVIDERS,
    SETUP_EXIT_CANCELLED,
    _format_init_command,
    handle_init,
    handle_init_feishu,
    handle_init_weixin,
)
from .config_editor import (
    ConfigUpdate,
    VOICE_NESTED_PROVIDERS,
    VOICE_PRESETS,
    image_generation_provider_needs_feature_key,
    load_config,
    prepare_image_generation_provider_update,
    prepare_model_provider_update,
    prepare_observability_update,
    prepare_search_provider_update,
    prepare_voice_interruptions_update,
    prepare_voice_nested_provider_update,
    prepare_voice_preset_update,
    prepare_voice_wake_update,
    provider_needs_feature_key,
    write_config,
)
from .overview import STATUS_DISABLED, RuntimeOverview, build_runtime_overview
from .terminal_ui import MenuOption, ReturnToLauncherHome, TerminalUI


def _launcher_options(*, initialized: bool, has_agents: bool = True) -> list[MenuOption]:
    setup_title = "Setup"
    setup_description = (
        "Review and update your current setup."
        if initialized
        else "Create config, identity, workspace, memory, and tasks."
    )
    options = [
        MenuOption(
            key="agent",
            title="Agents",
            description="Create, switch, or inspect managed agents.",
        ),
        MenuOption(
            key="channel",
            title="Channel",
            description="Open chat, API, Feishu, Weixin, and voice entry points.",
            disabled=not initialized,
        ),
        MenuOption(
            key="web",
            title="Web UI",
            description="Manage the browser web client.",
        ),
        MenuOption(
            key="inspect",
            title="Inspect",
            description="Read config, identity, memory, and message state.",
            disabled=not initialized,
        ),
        MenuOption(
            key="help",
            title="Help",
            description="Learn the common xAgent commands and when to use them.",
        ),
        MenuOption(
            key="exit",
            title="Exit",
            description="Close the launcher.",
        ),
    ]
    if has_agents:
        options.insert(
            4,
            MenuOption(
                key="setup",
                title=setup_title,
                description=setup_description,
            ),
        )
    return options


def _launcher_channel_options(config_dir: Path) -> list[MenuOption]:
    def _running(channel: str) -> bool:
        return running_pid(managed_paths(config_dir, channel).pid_path) is not None

    voice_running = _running(CHANNEL_VOICE)
    api_running = _running(CHANNEL_API)
    feishu_running = _running(CHANNEL_FEISHU)
    weixin_running = _running(CHANNEL_WEIXIN)

    return [
        MenuOption(
            key="chat",
            title="Chat",
            description="Talk with the configured agent in the terminal.",
        ),
        MenuOption(
            key="voice",
            title="Voice (running)" if voice_running else "Voice",
            description="Manage the microphone/speaker channel.",
        ),
        MenuOption(
            key=CHANNEL_API,
            title="API (running)" if api_running else "API",
            description="Manage the HTTP/WebSocket api channel.",
        ),
        MenuOption(
            key=CHANNEL_FEISHU,
            title="Feishu (running)" if feishu_running else "Feishu",
            description="Configure or manage the Feishu bot channel.",
        ),
        MenuOption(
            key=CHANNEL_WEIXIN,
            title="Weixin (running)" if weixin_running else "Weixin",
            description="Configure or manage the Weixin DM channel.",
        ),
        MenuOption(key="back", title="Back", description="Return to the main launcher."),
    ]


def _web_client_actions(config_dir: Path) -> list[MenuOption]:
    client_ready = _web_client_is_enabled(config_dir)
    client_running = client_ready and _web_client_is_running(config_dir)
    unavailable_description = "Enable web before starting the web client."
    if not client_ready:
        open_description = "Enable web before opening the browser client."
    elif not client_running:
        open_description = "Start the web client before opening it."
    else:
        open_description = "Open the running web client in your browser."

    return [
        MenuOption("open", "Open", open_description, disabled=not client_running),
        MenuOption(
            "start",
            "Start",
            "Start the web client." if client_ready else unavailable_description,
            disabled=not client_ready,
        ),
        MenuOption("stop", "Stop", "Stop the web client."),
        MenuOption(
            "restart",
            "Restart",
            "Restart the web client." if client_ready else unavailable_description,
            disabled=not client_ready,
        ),
        MenuOption("logs", "Logs", "View and follow the latest log output in real time."),
        MenuOption("back", "Back", "Return to the main launcher."),
    ]


def _run_web_action(config_dir: Path, action: str) -> int:
    if action == "open":
        return handle_web_open(_launcher_args(config_dir=str(config_dir)))
    if action == "start":
        return handle_web_start(
            _launcher_args(
                config_dir=str(config_dir),
                host=None,
                port=None,
                api_url=None,
                open_browser=False,
            )
        )
    if action == "stop":
        return handle_web_stop(_launcher_args(config_dir=str(config_dir)))
    if action == "restart":
        return handle_web_restart(
            _launcher_args(
                config_dir=str(config_dir),
                host=None,
                port=None,
                api_url=None,
                open_browser=False,
            )
        )
    if action == "logs":
        return handle_web_logs(
            _launcher_args(
                config_dir=str(config_dir),
                lines=80,
                follow=False,
            )
        )
    print(f"Unknown web client action: {action}")
    return 1


def _launcher_config_snapshot(config_dir: Path) -> dict[str, Any]:
    try:
        return load_config_file(config_dir)
    except Exception:
        return {}


def _feishu_channel_is_configured(config_dir: Path) -> bool:
    config = _launcher_config_snapshot(config_dir)
    data = feishu_config(config)
    return bool(data.get("app_id") and data.get("app_secret"))


def _weixin_channel_is_configured(config_dir: Path) -> bool:
    config = _launcher_config_snapshot(config_dir)
    data = weixin_config(config)
    return bool(data.get("account_id"))


def _api_channel_is_enabled(config_dir: Path) -> bool:
    config = _launcher_config_snapshot(config_dir)
    if not config:
        return False
    data = api_config(config)
    return bool(data.get("enabled", True))


def _api_channel_is_running(config_dir: Path) -> bool:
    return running_pid(managed_paths(config_dir, CHANNEL_API).pid_path) is not None


def _api_channel_url(config_dir: Path) -> str:
    config = _launcher_config_snapshot(config_dir)
    data = api_config(config)
    host = str(data.get("host") or BaseAgentConfig.DEFAULT_HOST).strip() or BaseAgentConfig.DEFAULT_HOST
    port = str(data.get("port") or BaseAgentConfig.DEFAULT_PORT).strip() or str(BaseAgentConfig.DEFAULT_PORT)
    browse_host = "127.0.0.1" if host == "0.0.0.0" else host
    if ":" in browse_host and not browse_host.startswith("["):
        browse_host = f"[{browse_host}]"
    return f"http://{browse_host}:{port}"


def _voice_is_configured(config: dict[str, Any]) -> bool:
    data = voice_config(config)
    return bool(data) and data.get("enabled") is not False


def _voice_channel_options(config: dict[str, Any]) -> list[MenuOption]:
    voice_enabled = _voice_is_configured(config)
    options: list[MenuOption] = []
    if not voice_enabled:
        options.append(MenuOption("setup", "Setup", "Configure voice mode."))
    start_description = (
        "Start the voice channel."
        if voice_enabled
        else "Set up voice first."
    )
    options.extend([
        MenuOption("start", "Start", start_description, disabled=not voice_enabled),
        MenuOption("stop", "Stop", "Stop the voice channel."),
        MenuOption(
            "restart",
            "Restart",
            "Restart the voice channel." if voice_enabled else "Set up voice first.",
            disabled=not voice_enabled,
        ),
        MenuOption("logs", "Logs", "View and follow the latest log output in real time."),
        MenuOption("devices", "List Devices", "Print available local audio input/output devices."),
        MenuOption("back", "Back", "Return to Channel."),
    ])
    return options


def _voice_resetup_options(config: dict[str, Any]) -> list[MenuOption]:
    voice_enabled = _voice_is_configured(config)
    interruptions_description = (
        "Enable or disable barge-in interruptions."
        if voice_enabled
        else "Enable voice first to update barge-in interruptions."
    )
    wake_description = (
        "Update wake mode, phrases, match, and idle timeout."
        if voice_enabled
        else "Enable voice first to update wake mode and phrases."
    )
    disable_description = (
        "Remove channels.voice from config."
        if voice_enabled
        else "Voice is already disabled."
    )
    return [
        MenuOption(
            "provider_mode",
            "Providers",
            "Use one provider for both STT and TTS, or configure separately.",
        ),
        MenuOption("interruptions", "Interruptions", interruptions_description, disabled=not voice_enabled),
        MenuOption("wake", "Wake", wake_description, disabled=not voice_enabled),
        MenuOption("disable", "Disable", disable_description, disabled=not voice_enabled),
        MenuOption("back", "Back", "Return to Edit Setup."),
    ]


def _observability_supported(config: dict[str, Any]) -> bool:
    return model_api_uses_openai_client(_current_model_api(config))


def _observability_resetup_options(config: dict[str, Any]) -> list[MenuOption]:
    enabled = bool(_current_observability(config).get("enabled", False))
    disable_description = (
        "Turn off Langfuse without deleting saved keys."
        if enabled
        else "Langfuse is already disabled."
    )
    return [
        MenuOption("enable", "Enable / Update", "Enable Langfuse and update its credentials."),
        MenuOption("disable", "Disable", disable_description, disabled=not enabled),
        MenuOption("back", "Back", "Return to Edit Setup."),
    ]


def _partial_update_options(config_dir: Path) -> list[MenuOption]:
    config = _launcher_config_snapshot(config_dir)
    observability_available = _observability_supported(config)
    observability_description = (
        "Enable or update Langfuse observability for OpenAI-compatible model APIs."
        if observability_available
        else "Requires an OpenAI-compatible model API. Update Model first."
    )
    return [
        MenuOption("model", "Model", "Update the main model provider, model, API key, or custom endpoint."),
        MenuOption("search", "Search", "Change provider-native web search."),
        MenuOption("voice", "Voice", "Enable or update STT/TTS, interruptions, and wake settings."),
        MenuOption("image", "Image", "Enable or update image generation provider settings."),
        MenuOption("feishu", "Feishu", "Re-run Feishu setup and replace channels.feishu."),
        MenuOption("weixin", "Weixin", "Re-run Weixin QR setup and replace channels.weixin."),
        MenuOption(
            "observability",
            "Observability",
            observability_description,
            disabled=not observability_available,
        ),
        MenuOption("back", "Back", "Return to Setup."),
    ]


def _web_client_is_enabled(config_dir: Path) -> bool:
    config = _launcher_config_snapshot(config_dir)
    return bool(web_client_config(config).get("enabled", True))


def _web_client_is_running(config_dir: Path) -> bool:
    del config_dir
    return running_pid(web_client_paths().pid_path) is not None


def _managed_channel_actions(config_dir: Path, channel: str) -> list[MenuOption]:
    actions: list[MenuOption] = []
    channel_ready = True
    unavailable_description = "Complete setup before starting this channel."
    if channel == CHANNEL_API:
        channel_ready = _api_channel_is_enabled(config_dir)
    if channel == CHANNEL_FEISHU and not _feishu_channel_is_configured(config_dir):
        channel_ready = False
        unavailable_description = "Configure channels.feishu before starting this channel."
        actions.append(
            MenuOption(
                "setup",
                "Setup",
                "Configure channels.feishu before starting this channel.",
            )
        )
    if channel == CHANNEL_WEIXIN and not _weixin_channel_is_configured(config_dir):
        channel_ready = False
        unavailable_description = "Configure channels.weixin before starting this channel."
        actions.append(
            MenuOption(
                "setup",
                "Setup",
                "Configure channels.weixin before starting this channel.",
            )
        )
    actions.extend([
        MenuOption(
            "start",
            "Start",
            "Start this channel." if channel_ready else unavailable_description,
            disabled=not channel_ready,
        ),
        MenuOption("stop", "Stop", "Stop this channel."),
        MenuOption(
            "restart",
            "Restart",
            "Restart this channel." if channel_ready else unavailable_description,
            disabled=not channel_ready,
        ),
        MenuOption("logs", "Logs", "View and follow the latest log output in real time."),
        MenuOption("back", "Back", "Return to Channel."),
    ])
    return actions


def _launcher_help_content(*, config_dir: Path, initialized: bool) -> Text:
    setup_command = "xagent setup --force" if initialized else "xagent setup"
    content = Text()
    content.append(f"Runtime: {config_dir}\n\n")

    content.append("Setup:\n")
    content.append("  ")
    content.append(_format_init_command(setup_command, config_dir=config_dir), style="cyan")
    content.append("\n    Configure the active agent.\n")
    content.append("  ")
    content.append(_format_init_command("xagent agents list", config_dir=config_dir), style="cyan")
    content.append("\n    List agents; use ")
    content.append(_format_init_command("xagent agents select <name>", config_dir=config_dir), style="cyan")
    content.append(" to switch.\n")

    content.append("\nUse Now:\n")
    content.append("  ")
    content.append(_format_init_command("xagent chat", config_dir=config_dir), style="cyan")
    content.append("\n    Chat in the terminal; use ")
    content.append(_format_init_command('xagent chat "Hey"', config_dir=config_dir), style="cyan")
    content.append(" for one message.\n")
    content.append("  ")
    content.append(_format_init_command("xagent api start", config_dir=config_dir), style="cyan")
    content.append("\n    Start the api channel.\n")
    content.append("  ")
    content.append(_format_init_command("xagent web start", config_dir=config_dir), style="cyan")
    content.append("\n    Start the browser web client.\n")
    content.append("  ")
    content.append(_format_init_command("xagent web open", config_dir=config_dir), style="cyan")
    content.append("\n    Open the running web client in your browser.\n")
    content.append("  ")
    content.append(_format_init_command("xagent voice start", config_dir=config_dir), style="cyan")
    content.append("\n    Start the voice channel.\n")
    content.append("  ")
    content.append(_format_init_command("xagent voice setup", config_dir=config_dir), style="cyan")
    content.append("\n    Configure the voice channel.\n")
    content.append("  ")
    content.append(_format_init_command("xagent status", config_dir=config_dir), style="cyan")
    content.append("\n    Show all configured channel processes.\n")
    content.append("  ")
    content.append(_format_init_command("xagent api logs -f", config_dir=config_dir), style="cyan")
    content.append("\n    Follow api channel logs.\n")
    content.append("  ")
    content.append(_format_init_command("xagent voice logs -f", config_dir=config_dir), style="cyan")
    content.append("\n    Follow voice logs.\n")

    content.append("\nIntegrations:\n")
    content.append("  ")
    content.append(_format_init_command("xagent feishu setup", config_dir=config_dir), style="cyan")
    content.append("\n    Configure Feishu, then start it with ")
    content.append(_format_init_command("xagent feishu start", config_dir=config_dir), style="cyan")
    content.append(".\n")
    content.append("  ")
    content.append(_format_init_command("xagent weixin setup", config_dir=config_dir), style="cyan")
    content.append("\n    Configure Weixin, then start it with ")
    content.append(_format_init_command("xagent weixin start", config_dir=config_dir), style="cyan")
    content.append(".\n")

    content.append("\nInspect:\n")
    content.append("  ")
    content.append(_format_init_command("xagent memory list --days 7", config_dir=config_dir), style="cyan")
    content.append("\n    Show recent daily journals; use ")
    content.append(_format_init_command("xagent memory search <query>", config_dir=config_dir), style="cyan")
    content.append(" to search.\n")
    content.append("  ")
    content.append(_format_init_command("xagent config show", config_dir=config_dir), style="cyan")
    content.append("\n    Print config.yaml; use ")
    content.append(_format_init_command("xagent config validate", config_dir=config_dir), style="cyan")
    content.append(" to check it.\n")
    content.append("  ")
    content.append(_format_init_command("xagent doctor", config_dir=config_dir), style="cyan")
    content.append("\n    Check local readiness.\n")
    return content


def _active_agent_context() -> tuple[str, Path, bool]:
    try:
        registry = load_agent_registry()
        entry = registry.agents[registry.active_agent]
        return entry.name, entry.path, True
    except AgentRegistryError:
        return "", management_root(), False


def _launcher_overview_subtitle(overview: RuntimeOverview) -> str:
    lines = [f"Runtime: {overview.config_dir}"]
    visible_items = [item for item in overview.items if item.name not in {"Config", "Identity", "Data"}]
    active_items = [item for item in visible_items if item.status != STATUS_DISABLED]
    disabled_names = [item.name for item in visible_items if item.status == STATUS_DISABLED]
    if active_items:
        lines.append("")
    for item in active_items:
        line = f"{item.name:<10} {item.value}"
        if item.name in {"API", "Web", "Voice", "Feishu", "Weixin"} and item.value == "running" and item.detail:
            line += f"  {item.detail}"
        lines.append(line)
    if disabled_names:
        lines.append("")
        lines.append(f"Not configured: {', '.join(disabled_names)}")
    return "\n".join(lines)


def _run_managed_channel_action(config_dir: Path, channel: str, action: str) -> int:
    channels = [channel]
    if action == "setup":
        if channel == CHANNEL_WEIXIN:
            return handle_init_weixin(
                _launcher_args(
                    config_dir=str(config_dir),
                    base_url=None,
                    cdn_base_url=None,
                    bot_type="3",
                    owner_only=True,
                    allow_users=None,
                    media_enabled=True,
                    force=False,
                    show_intro=False,
                    show_next_steps=False,
                )
            )
        return handle_init_feishu(
            _launcher_args(
                config_dir=str(config_dir),
                app_id=None,
                app_secret=None,
                manual=False,
                force=False,
                stream=None,
                group_fetch_limit=None,
                group_reply_only_when_mentioned=None,
                show_intro=False,
                show_next_steps=False,
            )
        )
    if action == "start":
        return handle_start(
            _launcher_args(
                config_dir=str(config_dir),
                channels=channels,
                host=None,
                port=None,
                open_browser=False,
                max_concurrent_chats=None,
                queue_timeout=None,
                chat_timeout=None,
            )
        )
    if action == "stop":
        return handle_stop(_launcher_args(config_dir=str(config_dir), channels=channels))
    if action == "restart":
        return handle_restart(
            _launcher_args(
                config_dir=str(config_dir),
                channels=channels,
                host=None,
                port=None,
                open_browser=False,
                max_concurrent_chats=None,
                queue_timeout=None,
                chat_timeout=None,
            )
        )
    if action == "status":
        return handle_status(
            _launcher_args(
                config_dir=str(config_dir),
                channels=channels,
                json_output=False,
            )
        )
    from .runtime import handle_logs

    print(f"\nFollowing {channel} logs (Ctrl+C to stop)...\n")
    try:
        return handle_logs(
            _launcher_args(
                config_dir=str(config_dir),
                channels=channels,
                lines=80,
                follow=True,
            )
        )
    except KeyboardInterrupt:
        print("\nStopped following logs.")
        return 0


def _current_search_provider(config: dict[str, Any]) -> str:
    search = config.get("search")
    if not isinstance(search, dict):
        return "none"
    return str(search.get("provider") or "none").strip().lower()


def _current_voice_provider(config: dict[str, Any]) -> str:
    voice = voice_config(config)
    if not voice:
        return "none"
    return str(voice.get("provider") or "custom").strip().lower()


def _voice_provider_mode_label(config: dict[str, Any]) -> str:
    return "Custom Providers" if _current_voice_provider(config) == "custom" else "Single Provider"


def _current_voice_nested_provider(config: dict[str, Any], section: str) -> str:
    voice = voice_config(config)
    nested = voice.get(section) if isinstance(voice, dict) else None
    if isinstance(nested, dict):
        provider = str(nested.get("provider") or "").strip().lower()
        if provider:
            return provider
    provider = str(voice.get("provider") or "").strip().lower() if isinstance(voice, dict) else ""
    return provider if provider in VOICE_NESTED_PROVIDERS else VOICE_NESTED_PROVIDERS[0]


def _current_voice_nested_api_key(config: dict[str, Any], section: str) -> str:
    voice = voice_config(config)
    nested = voice.get(section) if isinstance(voice, dict) else None
    if not isinstance(nested, dict):
        return ""
    return str(nested.get("api_key") or "").strip()


def _current_model_provider(config: dict[str, Any]) -> str:
    provider = config.get("provider")
    if not isinstance(provider, dict):
        return PROVIDER_OPENAI
    return normalize_provider_name(provider.get("name")) or PROVIDER_OPENAI


def _current_model_api(config: dict[str, Any]) -> str:
    provider = config.get("provider")
    if not isinstance(provider, dict):
        return MODEL_API_OPENAI_CHAT_COMPLETIONS
    try:
        return provider_model_api(provider)
    except Exception:
        return MODEL_API_OPENAI_CHAT_COMPLETIONS


def _current_image_generation_provider(config: dict[str, Any]) -> str:
    image_generation = config.get("image_generation")
    if not isinstance(image_generation, dict):
        return "none"
    return str(image_generation.get("provider") or "none").strip().lower()


def _current_observability(config: dict[str, Any]) -> dict[str, Any]:
    observability = config.get("observability")
    return dict(observability) if isinstance(observability, dict) else {}


def _model_options_for_provider(provider: str) -> tuple[str, ...]:
    if provider == PROVIDER_OPENAI:
        return OPENAI_MODELS
    if provider == PROVIDER_ANTHROPIC:
        return ANTHROPIC_MODELS
    if provider == PROVIDER_DEEPSEEK:
        return DEEPSEEK_MODELS
    if provider == PROVIDER_MINIMAX:
        return MINIMAX_MODELS
    if provider == PROVIDER_QWEN:
        return QWEN_MODELS
    return (MODEL_PLACEHOLDER,)


def _default_model_index(options: Sequence[str], current_model: str) -> int:
    try:
        return list(options).index(current_model)
    except ValueError:
        return 0


def _phrase_list(raw_value: str) -> list[str]:
    return [item.strip() for item in raw_value.split(",") if item.strip()]


def _phrase_summary(value: Any) -> str:
    if not isinstance(value, (list, tuple)):
        return "(none)"
    items = [str(item).strip() for item in value if str(item).strip()]
    return ", ".join(items) if items else "(none)"


def _voice_wake_subtitle(wake: dict[str, Any]) -> str:
    return (
        f"Enabled: {bool(wake.get('enabled', False))}\n"
        f"Match: {wake.get('match_mode', 'prefix')}\n"
        f"Idle timeout: {wake.get('idle_timeout_seconds', 60)}s\n"
        f"Wake phrases: {_phrase_summary(wake.get('wake_phrases'))}\n"
        f"Exit phrases: {_phrase_summary(wake.get('exit_phrases'))}"
    )


def _voice_summary_subtitle(config: dict[str, Any]) -> str:
    mode = _voice_provider_mode_label(config)
    if mode == "Custom Providers":
        voice = voice_config(config)
        stt_provider = _current_voice_nested_provider(config, "stt") if voice else "none"
        tts_provider = _current_voice_nested_provider(config, "tts") if voice else "none"
        return f"Provider mode: {mode}\nSTT: {stt_provider}\nTTS: {tts_provider}"
    return f"Provider mode: {mode}\nProvider: {_current_voice_provider(config)}"


def _feishu_config_subtitle(config_dir: Path) -> str:
    if not _feishu_channel_is_configured(config_dir):
        return "Feishu is not configured yet."
    data = feishu_config(_launcher_config_snapshot(config_dir))
    app_id = str(data.get("app_id") or "").strip()
    return f"Configured App ID: {app_id}" if app_id else "Feishu is configured."


def _weixin_config_subtitle(config_dir: Path) -> str:
    if not _weixin_channel_is_configured(config_dir):
        return "Weixin is not configured yet."
    data = weixin_config(_launcher_config_snapshot(config_dir))
    account_id = str(data.get("account_id") or "").strip()
    return f"Configured account: {account_id}" if account_id else "Weixin is configured."


def _launcher_feishu_setup_args(config_dir: Path):
    return _launcher_args(
        config_dir=str(config_dir),
        app_id=None,
        app_secret=None,
        manual=False,
        force=_feishu_channel_is_configured(config_dir),
        stream=None,
        group_fetch_limit=None,
        group_reply_only_when_mentioned=None,
        show_intro=False,
        show_next_steps=False,
    )


def _launcher_weixin_setup_args(config_dir: Path):
    return _launcher_args(
        config_dir=str(config_dir),
        base_url=None,
        cdn_base_url=None,
        bot_type="3",
        owner_only=True,
        allow_users=None,
        media_enabled=True,
        force=_weixin_channel_is_configured(config_dir),
        show_intro=False,
        show_next_steps=False,
    )


def _run_feishu_config_launcher(ui: TerminalUI, config_dir: Path) -> None:
    while True:
        option = ui.select_menu(
            title="xAgent Setup / Feishu",
            subtitle=_feishu_config_subtitle(config_dir),
            options=[
                MenuOption(
                    "setup",
                    "Configure",
                    "Create or replace the Feishu bot credentials.",
                ),
                MenuOption("back", "Back", "Return to Edit Setup."),
            ],
            footer="↑/↓ Move • Enter Select  •  q Back",
        )
        if option is None or option.key == "back":
            return
        ui.clear()
        result = handle_init_feishu(_launcher_feishu_setup_args(config_dir))
        if result == SETUP_EXIT_CANCELLED:
            continue
        if result == 0:
            ui.pause("Press Enter to return to the launcher")
            raise ReturnToLauncherHome()


def _run_weixin_config_launcher(ui: TerminalUI, config_dir: Path) -> None:
    while True:
        option = ui.select_menu(
            title="xAgent Setup / Weixin",
            subtitle=_weixin_config_subtitle(config_dir),
            options=[
                MenuOption(
                    "setup",
                    "Configure",
                    "Scan WeChat to create or refresh the Weixin DM channel.",
                ),
                MenuOption("back", "Back", "Return to Edit Setup."),
            ],
            footer="↑/↓ Move • Enter Select  •  q Back",
        )
        if option is None or option.key == "back":
            return
        ui.clear()
        result = handle_init_weixin(_launcher_weixin_setup_args(config_dir))
        if result == SETUP_EXIT_CANCELLED:
            continue
        if result == 0:
            ui.pause("Press Enter to return to the launcher")
            raise ReturnToLauncherHome()


def _run_managed_channel_launcher(
    ui: TerminalUI,
    config_dir: Path,
    channel: str,
    *,
    channel_title: str,
) -> None:
    while True:
        option = ui.select_menu(
            title=f"xAgent Channel / {channel_title}",
            subtitle=f"Runtime: {config_dir}",
            options=_managed_channel_actions(config_dir, channel),
            footer="↑/↓ Move • Enter Select  •  q Back",
        )
        if option is None or option.key == "back":
            ui.clear()
            return
        ui.clear()
        exit_code = _run_managed_channel_action(config_dir, channel, str(option.key))
        if exit_code == SETUP_EXIT_CANCELLED:
            continue
        if exit_code != 0:
            ui.print_panel(f"Channel action exited with status {exit_code}.", title="Channel")
        ui.pause(f"Press Enter to return to {channel_title}")


def _existing_voice_provider_api_key(config: dict[str, Any], provider: str) -> Optional[str]:
    voice = voice_config(config)
    if not voice:
        return None
    for section in ("stt", "tts"):
        nested = voice.get(section)
        if not isinstance(nested, dict):
            continue
        nested_provider = str(nested.get("provider") or voice.get("provider") or "").strip().lower()
        api_key = str(nested.get("api_key") or "").strip()
        if nested_provider == provider and api_key and not is_placeholder_api_key(api_key):
            return api_key
    return None


def _feature_api_key_available(config: dict[str, Any], section: str, provider: str) -> bool:
    feature = config.get(section)
    if isinstance(feature, dict):
        feature_provider = normalize_provider_name(feature.get("provider"))
        configured_key = str(feature.get("api_key") or "").strip()
        if feature_provider == normalize_provider_name(provider) and configured_key and not is_placeholder_api_key(configured_key):
            return True

    provider_cfg = config.get("provider")
    if isinstance(provider_cfg, dict) and normalize_provider_name(provider_cfg.get("name")) == normalize_provider_name(provider):
        provider_key = str(provider_cfg.get("api_key") or "").strip()
        if provider_key and not is_placeholder_api_key(provider_key):
            return True

    return False


def _provider_option_descriptions(kind: str) -> dict[str, str]:
    return {
        "none": f"Disable {kind}.",
        "openai": "Use OpenAI.",
        "anthropic": "Use Anthropic.",
        "deepseek": "Use DeepSeek.",
        "qwen": "Use Qwen / DashScope.",
        "minimax": "Use MiniMax.",
        "soniox": "Use Soniox realtime voice.",
        "custom": "Choose STT and TTS providers separately.",
    }


def _config_update_content(update: ConfigUpdate) -> Text:
    content = Text()
    content.append("Apply these changes?\n\n")
    if not update.changes:
        content.append("No config values will change.\n")
        return content
    for change in update.changes:
        content.append(f"{change.path}\n", style="bold")
        content.append(f"  {change.before} -> {change.after}\n")
    return content


def _apply_config_update(
    ui: TerminalUI,
    config_dir: Path,
    update: ConfigUpdate,
    *,
    return_home_on_success: bool = False,
) -> bool:
    if not update.changes:
        ui.print_panel("No config values changed.", title="Setup")
        return False
    ui.print_panel(_config_update_content(update), title="Config Preview")
    if ui.confirm("Apply changes?", default=True) is not True:
        ui.print_panel("Config was not changed.", title="Setup")
        return False
    try:
        write_config(config_dir, update.data)
    except Exception as exc:
        ui.print_panel(f"Config save failed: {exc}", title="Setup", border_style="red")
        return False
    ui.print_panel("Config saved. Validation passed.", title="Setup", border_style="green")
    if return_home_on_success:
        raise ReturnToLauncherHome()
    return True


def _required_feature_api_key(ui: TerminalUI, *, provider: str, feature: str) -> Optional[str]:
    subtitle = f"xAgent stores a separate API key for {provider.title()} {feature} in config."
    if feature.lower().startswith("voice "):
        subtitle = f"{provider.title()} {feature} needs its own API key for the current model provider."
    value = ui.ask_text(
        f"{provider.title()} API key",
        secret=True,
        subtitle=subtitle,
    ).strip()
    if not value:
        ui.print_panel("API key is required for this change.", title="Setup")
        return None
    return value


def _run_search_config_launcher(ui: TerminalUI, config_dir: Path) -> bool:
    try:
        config = load_config(config_dir)
    except Exception as exc:
        ui.print_panel(f"Cannot load config: {exc}", title="Setup", border_style="red")
        return True
    current = _current_search_provider(config)
    options = _menu_option_rows(
        SEARCH_PROVIDERS + ("back",),
        _provider_option_descriptions("search"),
    )
    choice = ui.select_menu(
        title="xAgent Setup / Search",
        subtitle=f"Current provider: {current}",
        options=options,
        footer="↑/↓ Move • Enter Select  •  q Back",
    )
    if choice is None or choice.key == "back":
        return False
    api_key = None
    if provider_needs_feature_key(config, choice.key):
        current_key = str((config.get("search") or {}).get("api_key") or "") if isinstance(config.get("search"), dict) else ""
        if choice.key != current or is_placeholder_api_key(current_key):
            api_key = _required_feature_api_key(ui, provider=choice.key, feature="search")
            if api_key is None:
                return True
    try:
        update = prepare_search_provider_update(config, provider=choice.key, api_key=api_key)
    except Exception as exc:
        ui.print_panel(f"Search update is invalid: {exc}", title="Setup", border_style="red")
        return True
    return _apply_config_update(ui, config_dir, update, return_home_on_success=True)


def _run_model_config_launcher(ui: TerminalUI, config_dir: Path) -> bool:
    try:
        config = load_config(config_dir)
    except Exception as exc:
        ui.print_panel(f"Cannot load config: {exc}", title="Setup", border_style="red")
        return True

    current_provider = _current_model_provider(config)
    provider_choice = ui.select_menu(
        title="xAgent Setup / Model",
        subtitle=f"Current provider: {current_provider}",
        options=_menu_option_rows(KNOWN_PROVIDERS + ("back",), _provider_option_descriptions("model")),
        footer="↑/↓ Move • Enter Select  •  q Back",
    )
    if provider_choice is None or provider_choice.key == "back":
        return False
    provider = str(provider_choice.key)
    provider_config = config.get("provider") if isinstance(config.get("provider"), dict) else {}
    current_model = str(provider_config.get("model") or "").strip()

    model_api = None
    base_url = None
    supports_vision = None
    if provider == PROVIDER_CUSTOM:
        api_choice = ui.select_menu(
            title="xAgent Setup / Model API",
            subtitle=f"Current API: {_current_model_api(config)}",
            options=_menu_option_rows((
                "openai_chat_completions",
                "openai_responses",
                "anthropic_messages",
                "back",
            )),
            footer="↑/↓ Move • Enter Select  •  q Back",
        )
        if api_choice is None or api_choice.key == "back":
            return False
        model_api = str(api_choice.key)
        default_base_url = str(provider_config.get("base_url") or provider_base_url(PROVIDER_OPENAI if provider == PROVIDER_OPENAI else provider, model_api))
        ui.clear()
        base_url = ui.ask_text("Custom provider base URL", default=default_base_url).strip() or default_base_url
        supports_vision = ui.confirm(
            "Does this custom provider support image URL input?",
            default=bool(provider_config.get("supports_vision", False)),
        )
        if supports_vision is None:
            return False
        model = ui.ask_text("Model", default=current_model or MODEL_PLACEHOLDER).strip() or MODEL_PLACEHOLDER
    else:
        model_options = _model_options_for_provider(provider)
        model = _terminal_select_model_option(
            ui,
            "Model",
            model_options,
            default_index=_default_model_index(model_options, current_model),
            subtitle=f"Choose a {provider} model.",
        )
        base_url = provider_base_url(provider)

    api_key = ui.ask_text(
        f"{provider.title()} API key",
        secret=True,
        subtitle="Leave blank to keep the existing key or fill it later.",
    ).strip() or None

    search_api_key = None
    search_provider = _current_search_provider(config)
    if search_provider != "none" and search_provider != provider and not _feature_api_key_available(config, "search", search_provider):
        search_api_key = _required_feature_api_key(ui, provider=search_provider, feature="search")
        if search_api_key is None:
            return True

    image_generation_api_key = None
    image_provider = _current_image_generation_provider(config)
    if image_provider != "none" and image_provider != provider and not _feature_api_key_available(config, "image_generation", image_provider):
        image_generation_api_key = _required_feature_api_key(ui, provider=image_provider, feature="image generation")
        if image_generation_api_key is None:
            return True

    try:
        update = prepare_model_provider_update(
            config,
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=base_url,
            model_api=model_api,
            supports_vision=supports_vision,
            search_api_key=search_api_key,
            image_generation_api_key=image_generation_api_key,
        )
    except Exception as exc:
        ui.print_panel(f"Model update is invalid: {exc}", title="Setup", border_style="red")
        return True
    return _apply_config_update(ui, config_dir, update, return_home_on_success=True)


def _terminal_select_model_option(ui: TerminalUI, title: str, options: Sequence[str], *, default_index: int = 0, subtitle: str = "") -> str:
    rows = _model_option_rows(options)
    choice = ui.select(label=title, subtitle=subtitle, options=rows, default_index=default_index)
    if choice is None:
        raise KeyboardInterrupt()
    if choice.key == CUSTOM_MODEL_OPTION:
        return ui.ask_text("Custom model name", default=MODEL_PLACEHOLDER).strip() or MODEL_PLACEHOLDER
    if choice.key == "Decide later":
        return MODEL_PLACEHOLDER
    return choice.key


def _menu_option_rows(options: Sequence[str], descriptions: Optional[dict[str, str]] = None) -> list[MenuOption]:
    option_descriptions = descriptions or {}
    return [
        MenuOption(
            key=option,
            title=option,
            description=option_descriptions.get(option, f"Use {option}."),
        )
        for option in options
    ]


def _model_option_rows(options: Sequence[str], descriptions: Optional[dict[str, str]] = None) -> list[MenuOption]:
    option_descriptions = dict(descriptions or {})
    option_descriptions[CUSTOM_MODEL_OPTION] = "Enter a custom model name now."
    values = list(options)
    if CUSTOM_MODEL_OPTION not in values:
        values.append(CUSTOM_MODEL_OPTION)
    return _menu_option_rows(tuple(values), option_descriptions)


def _run_image_generation_config_launcher(ui: TerminalUI, config_dir: Path) -> bool:
    try:
        config = load_config(config_dir)
    except Exception as exc:
        ui.print_panel(f"Cannot load config: {exc}", title="Setup", border_style="red")
        return True
    current = _current_image_generation_provider(config)
    choice = ui.select_menu(
        title="xAgent Setup / Image Generation",
        subtitle=f"Current provider: {current}",
        options=_menu_option_rows(IMAGE_GENERATION_PROVIDERS + ("back",), _provider_option_descriptions("image generation")),
        footer="↑/↓ Move • Enter Select  •  q Back",
    )
    if choice is None or choice.key == "back":
        return False
    api_key = None
    if image_generation_provider_needs_feature_key(config, choice.key):
        current_key = ""
        image_generation = config.get("image_generation")
        if isinstance(image_generation, dict):
            current_key = str(image_generation.get("api_key") or "")
        if choice.key != current or is_placeholder_api_key(current_key):
            api_key = _required_feature_api_key(ui, provider=choice.key, feature="image generation")
            if api_key is None:
                return True
    try:
        update = prepare_image_generation_provider_update(config, provider=choice.key, api_key=api_key)
    except Exception as exc:
        ui.print_panel(f"Image generation update is invalid: {exc}", title="Setup", border_style="red")
        return True
    return _apply_config_update(ui, config_dir, update, return_home_on_success=True)


def _run_observability_config_launcher(ui: TerminalUI, config_dir: Path) -> bool:
    try:
        config = load_config(config_dir)
    except Exception as exc:
        ui.print_panel(f"Cannot load config: {exc}", title="Setup", border_style="red")
        return True

    model_api = _current_model_api(config)
    if not model_api_uses_openai_client(model_api):
        ui.print_panel(
            "Langfuse observability requires an OpenAI-compatible model API.\n"
            "Update Model to OpenAI, Responses, or a compatible custom endpoint first.",
            title="Setup",
        )
        return True

    observability = _current_observability(config)
    enabled = bool(observability.get("enabled", False))
    provider = str(observability.get("provider") or "langfuse").strip().lower() if enabled else "none"
    current_base_url = str(observability.get("base_url") or LANGFUSE_BASE_URL).strip() or LANGFUSE_BASE_URL

    choice = ui.select_menu(
        title="xAgent Setup / Observability",
        subtitle=f"Status: {'enabled' if enabled else 'not enabled'}\nProvider: {provider}\nBase URL: {current_base_url}",
        options=_observability_resetup_options(config),
        footer="↑/↓ Move • Enter Select  •  q Back",
    )
    if choice is None or choice.key == "back":
        return False

    try:
        if choice.key == "disable":
            update = prepare_observability_update(config, enabled=False)
        else:
            current_public_key = str(observability.get("public_key") or "").strip()
            current_secret_key = str(observability.get("secret_key") or "").strip()
            public_key = (
                ui.ask_text(
                    "Langfuse public key",
                    default=current_public_key or LANGFUSE_PUBLIC_KEY_PLACEHOLDER,
                    subtitle="Leave the placeholder if you want to fill this in later.",
                ).strip()
                or current_public_key
                or LANGFUSE_PUBLIC_KEY_PLACEHOLDER
            )
            secret_key = ui.ask_secret("Langfuse secret key").strip() or current_secret_key or LANGFUSE_SECRET_KEY_PLACEHOLDER
            base_url = (
                ui.ask_text(
                    "Langfuse base URL",
                    default=current_base_url,
                ).strip()
                or current_base_url
            )
            update = prepare_observability_update(
                config,
                enabled=True,
                public_key=public_key,
                secret_key=secret_key,
                base_url=base_url,
            )
    except Exception as exc:
        ui.print_panel(f"Observability update is invalid: {exc}", title="Setup", border_style="red")
        return True

    return _apply_config_update(ui, config_dir, update, return_home_on_success=True)


def _run_voice_single_provider_config(ui: TerminalUI, config_dir: Path) -> None:
    try:
        config = load_config(config_dir)
    except Exception as exc:
        ui.print_panel(f"Cannot load config: {exc}", title="Setup", border_style="red")
        return
    current = _current_voice_provider(config)
    subtitle = f"Current provider: {current}"
    if current == "custom":
        subtitle = "Current mode: Custom Providers\nChoose one provider for both STT and TTS."
    choice = ui.select_menu(
        title="xAgent Setup / Voice Single Provider",
        subtitle=subtitle,
        options=_menu_option_rows(
            tuple(provider for provider in VOICE_PRESETS if provider != "custom") + ("back",),
            _provider_option_descriptions("voice"),
        ),
        footer="↑/↓ Move • Enter Select  •  q Back",
    )
    if choice is None or choice.key == "back":
        return
    api_key = None
    if choice.key in VOICE_NESTED_PROVIDERS:
        existing_key = _existing_voice_provider_api_key(config, choice.key)
        has_existing = bool(existing_key)
        subtitle = "Leave blank to keep the existing key." if has_existing else ""
        value = ui.ask_text(
            f"{choice.key.title()} API key",
            secret=True,
            subtitle=subtitle,
        ).strip()
        if value:
            api_key = value
        elif not has_existing:
            ui.print_panel("API key is required for this change.", title="Setup")
            return
    try:
        update = prepare_voice_preset_update(config, provider=choice.key, api_key=api_key)
    except Exception as exc:
        ui.print_panel(f"Voice update is invalid: {exc}", title="Setup", border_style="red")
        return
    _apply_config_update(ui, config_dir, update, return_home_on_success=True)


def _run_voice_custom_provider_launcher(ui: TerminalUI, config_dir: Path) -> None:
    while True:
        try:
            config = load_config(config_dir)
        except Exception as exc:
            ui.print_panel(f"Cannot load config: {exc}", title="Setup", border_style="red")
            return
        voice = voice_config(config)
        stt_provider = _current_voice_nested_provider(config, "stt") if voice else "none"
        tts_provider = _current_voice_nested_provider(config, "tts") if voice else "none"
        option = ui.select_menu(
            title="xAgent Setup / Voice Custom Providers",
            subtitle=f"STT: {stt_provider}\nTTS: {tts_provider}",
            options=[
                MenuOption("stt", "STT Provider", "Choose the speech-to-text provider."),
                MenuOption("tts", "TTS Provider", "Choose the text-to-speech provider."),
                MenuOption("back", "Back", "Return to Provider Mode."),
            ],
            footer="↑/↓ Move • Enter Select  •  q Back",
        )
        if option is None or option.key == "back":
            return
        ui.clear()
        _run_voice_nested_config(ui, config_dir, config, option.key)


def _run_voice_provider_mode_launcher(ui: TerminalUI, config_dir: Path, config: dict[str, Any]) -> None:
    del config
    while True:
        try:
            current_config = load_config(config_dir)
        except Exception as exc:
            ui.print_panel(f"Cannot load config: {exc}", title="Setup", border_style="red")
            return
        option = ui.select_menu(
            title="xAgent Setup / Voice Provider Mode",
            subtitle=_voice_summary_subtitle(current_config),
            options=[
                MenuOption("single", "Single Provider", "Use the same provider for STT and TTS."),
                MenuOption("custom", "Custom Providers", "Configure STT and TTS independently."),
                MenuOption("back", "Back", "Return to Voice."),
            ],
            footer="↑/↓ Move • Enter Select  •  q Back",
        )
        if option is None or option.key == "back":
            return
        ui.clear()
        if option.key == "single":
            _run_voice_single_provider_config(ui, config_dir)
        else:
            _run_voice_custom_provider_launcher(ui, config_dir)


def _run_voice_nested_config(ui: TerminalUI, config_dir: Path, config: dict[str, Any], section: str) -> None:
    current = _current_voice_nested_provider(config, section)
    title = "STT Provider" if section == "stt" else "TTS Provider"
    choice = ui.select_menu(
        title=f"xAgent Setup / Voice {title}",
        subtitle=f"Current {section.upper()}: {current}",
        options=_menu_option_rows(VOICE_NESTED_PROVIDERS + ("back",), _provider_option_descriptions("voice")),
        footer="↑/↓ Move • Enter Select  •  q Back",
    )
    if choice is None or choice.key == "back":
        return
    api_key = None
    if choice.key in VOICE_NESTED_PROVIDERS:
        current_key = _current_voice_nested_api_key(config, section)
        has_existing = choice.key == current and bool(current_key) and not is_placeholder_api_key(current_key)
        section_label = "STT" if section == "stt" else "TTS"
        subtitle = "Leave blank to keep the existing key." if has_existing else ""
        value = ui.ask_text(
            f"{choice.key.title()} API key",
            secret=True,
            subtitle=subtitle,
        ).strip()
        if value:
            api_key = value
        elif not has_existing:
            ui.print_panel("API key is required for this change.", title="Setup")
            return
    try:
        update = prepare_voice_nested_provider_update(config, section=section, provider=choice.key, api_key=api_key)
    except Exception as exc:
        ui.print_panel(f"Voice update is invalid: {exc}", title="Setup", border_style="red")
        return
    _apply_config_update(ui, config_dir, update, return_home_on_success=True)


def _run_voice_interruptions_config(ui: TerminalUI, config_dir: Path, config: dict[str, Any]) -> None:
    voice = voice_config(config)
    if not voice:
        ui.print_panel("Enable voice before updating interruptions.", title="Setup")
        return
    current = bool(voice.get("enable_interruptions", False))
    enabled = ui.confirm("Enable voice interruptions?", default=current)
    if enabled is None:
        return
    try:
        update = prepare_voice_interruptions_update(config, enabled=enabled)
    except Exception as exc:
        ui.print_panel(f"Voice update is invalid: {exc}", title="Setup", border_style="red")
        return
    _apply_config_update(ui, config_dir, update, return_home_on_success=True)


def _run_voice_wake_config_launcher(ui: TerminalUI, config_dir: Path) -> None:
    while True:
        try:
            config = load_config(config_dir)
        except Exception as exc:
            ui.print_panel(f"Cannot load config: {exc}", title="Setup", border_style="red")
            return
        voice = voice_config(config)
        if not voice:
            ui.print_panel("Enable voice before updating wake settings.", title="Setup")
            return
        wake = voice.get("wake") if isinstance(voice.get("wake"), dict) else {}
        option = ui.select_menu(
            title="xAgent Setup / Voice Wake",
            subtitle=_voice_wake_subtitle(wake),
            options=[
                MenuOption("enabled", "Wake Mode", "Enable or disable wake phrase gating."),
                MenuOption("wake_phrases", "Wake Phrases", "Comma-separated phrases that start listening."),
                MenuOption("exit_phrases", "Exit Phrases", "Comma-separated phrases that stop the session."),
                MenuOption("match_mode", "Match Mode", "Choose prefix or contains matching."),
                MenuOption("idle_timeout", "Idle Timeout", "Seconds before an idle wake session closes."),
                MenuOption("back", "Back", "Return to Voice."),
            ],
            footer="↑/↓ Move • Enter Select  •  q Back",
        )
        if option is None or option.key == "back":
            return
        ui.clear()

        try:
            if option.key == "enabled":
                enabled = ui.confirm("Enable wake phrase gating?", default=bool(wake.get("enabled", False)))
                if enabled is None:
                    continue
                update = prepare_voice_wake_update(config, enabled=enabled)
            elif option.key == "wake_phrases":
                raw_value = ui.ask_text(
                    "Wake phrases",
                    default=", ".join(wake.get("wake_phrases") or ["xAgent"]),
                    subtitle="Separate phrases with commas.",
                )
                update = prepare_voice_wake_update(config, wake_phrases=_phrase_list(raw_value))
            elif option.key == "exit_phrases":
                raw_value = ui.ask_text(
                    "Exit phrases",
                    default=", ".join(wake.get("exit_phrases") or ["exit", "stop"]),
                    subtitle="Separate phrases with commas.",
                )
                update = prepare_voice_wake_update(config, exit_phrases=_phrase_list(raw_value))
            elif option.key == "match_mode":
                current = str(wake.get("match_mode") or "prefix")
                mode_choice = ui.select_menu(
                    title="xAgent Setup / Voice Wake Match",
                    subtitle=f"Current match mode: {current}",
                    options=_menu_option_rows(("prefix", "contains", "back")),
                    footer="↑/↓ Move • Enter Select  •  q Back",
                )
                if mode_choice is None or mode_choice.key == "back":
                    continue
                update = prepare_voice_wake_update(config, match_mode=mode_choice.key)
            else:
                raw_value = ui.ask_text(
                    "Idle timeout seconds",
                    default=str(wake.get("idle_timeout_seconds") or 60),
                    subtitle="Enter a number between 0.1 and 3600.",
                ).strip()
                idle_timeout_seconds = float(raw_value)
                update = prepare_voice_wake_update(config, idle_timeout_seconds=idle_timeout_seconds)
        except Exception as exc:
            ui.print_panel(f"Voice wake update is invalid: {exc}", title="Setup", border_style="red")
            continue
        _apply_config_update(ui, config_dir, update, return_home_on_success=True)


def _run_voice_config_launcher(ui: TerminalUI, config_dir: Path) -> None:
    while True:
        try:
            config = load_config(config_dir)
        except Exception as exc:
            ui.print_panel(f"Cannot load config: {exc}", title="Setup", border_style="red")
            return
        option = ui.select_menu(
            title="xAgent Setup / Voice",
            subtitle=_voice_summary_subtitle(config),
            options=_voice_resetup_options(config),
            footer="↑/↓ Move • Enter Select  •  q Back",
        )
        if option is None or option.key == "back":
            return
        ui.clear()
        if option.key == "provider_mode":
            _run_voice_provider_mode_launcher(ui, config_dir, config)
        elif option.key == "interruptions":
            _run_voice_interruptions_config(ui, config_dir, config)
        elif option.key == "wake":
            _run_voice_wake_config_launcher(ui, config_dir)
        elif option.key == "disable":
            try:
                update = prepare_voice_preset_update(config, provider="none")
            except Exception as exc:
                ui.print_panel(f"Voice update is invalid: {exc}", title="Setup", border_style="red")
                continue
            _apply_config_update(ui, config_dir, update, return_home_on_success=True)


def _run_partial_update_launcher(ui: TerminalUI, config_dir: Path) -> None:
    while True:
        overview = build_runtime_overview(config_dir)
        option = ui.select_menu(
            title="xAgent Setup / Edit Setup",
            subtitle=_launcher_overview_subtitle(overview),
            options=_partial_update_options(config_dir),
            footer="↑/↓ Move • Enter Select  •  q Back",
        )
        if option is None or option.key == "back":
            ui.clear()
            return
        ui.clear()
        should_pause = False
        if option.key == "model":
            should_pause = _run_model_config_launcher(ui, config_dir)
        elif option.key == "observability":
            should_pause = _run_observability_config_launcher(ui, config_dir)
        elif option.key == "search":
            should_pause = _run_search_config_launcher(ui, config_dir)
        elif option.key == "feishu":
            _run_feishu_config_launcher(ui, config_dir)
        elif option.key == "weixin":
            _run_weixin_config_launcher(ui, config_dir)
        elif option.key == "voice":
            _run_voice_config_launcher(ui, config_dir)
        elif option.key == "image":
            should_pause = _run_image_generation_config_launcher(ui, config_dir)
        if should_pause is True:
            ui.pause("Press Enter to return to Edit Setup")


def _run_resetup_launcher(config_dir: Path) -> int:
    ui = TerminalUI()
    try:
        while True:
            overview = build_runtime_overview(config_dir)
            option = ui.select_menu(
                title="xAgent Setup",
                subtitle=_launcher_overview_subtitle(overview),
                options=[
                    MenuOption("partial", "Edit Setup", "Update one configured feature without editing YAML."),
                    MenuOption("full", "Full Setup", "Run the full setup flow again."),
                    MenuOption("back", "Back", "Return to the main launcher."),
                ],
                footer="↑/↓ Move • Enter Select  •  q Back",
            )
            if option is None or option.key == "back":
                ui.clear()
                return 0
            ui.clear()
            if option.key == "full":
                exit_code = handle_init(_launcher_args(config_dir=str(config_dir), force=True, schema=False))
                if exit_code == 0:
                    ui.clear()
                    return 0
                ui.pause("Press Enter to return to Setup")
            elif option.key == "partial":
                _run_partial_update_launcher(ui, config_dir)
    except ReturnToLauncherHome:
        ui.clear()
        return 0


def _print_skills_summary(config_dir: Path) -> int:
    root = config_dir / BaseAgentConfig.SKILLS_DIRNAME
    if not root.exists():
        print(f"Skills root: {root}")
        print("Skills: not found")
        return 0

    from ...components.skills import SkillsStorageLocal

    storage = SkillsStorageLocal(root, seed_builtins=False)
    info = storage.info()
    print(f"Skills root: {info['root']}")
    print(f"Total: {info['count']}")
    print(f"Enabled: {info['enabled_count']}")
    print(f"Disabled: {info['disabled_count']}")
    print(f"Invalid: {info['invalid_count']}")
    return 0


def _print_skills_list(config_dir: Path) -> int:
    root = config_dir / BaseAgentConfig.SKILLS_DIRNAME
    if not root.exists():
        print(f"Skills root: {root}")
        print("No skills found.")
        return 0

    from ...components.skills import SkillsStorageLocal

    storage = SkillsStorageLocal(root, seed_builtins=False)
    skills = storage.list_skills(include_disabled=True, include_invalid=True)
    if not skills:
        print("No skills found.")
        return 0
    for skill in skills:
        state = "enabled" if skill.enabled else "disabled"
        validity = "valid" if skill.valid else "invalid"
        print(f"{skill.name} [{state}, {validity}]")
        print(f"  file: {skill.skill_file}")
        if skill.description:
            print(f"  description: {skill.description}")
    return 0


def _print_skills_search(config_dir: Path, query: str) -> int:
    query = query.strip()
    if not query:
        print("Search query is required.")
        return 1
    root = config_dir / BaseAgentConfig.SKILLS_DIRNAME
    if not root.exists():
        print("No skills found.")
        return 0

    from ...components.skills import SkillsStorageLocal

    storage = SkillsStorageLocal(root, seed_builtins=False)
    results = storage.search(query).get("results", [])
    if not results:
        print("No matching skill files.")
        return 0
    for item in results:
        print(item.get("path", ""))
        snippet = str(item.get("snippet") or "").strip()
        if snippet:
            print(f"  {snippet}")
    return 0


def _print_skills_validation(config_dir: Path) -> int:
    root = config_dir / BaseAgentConfig.SKILLS_DIRNAME
    if not root.exists():
        print(f"Skills root: {root}")
        print("No skills found.")
        return 0

    from ...components.skills import SkillsStorageLocal

    storage = SkillsStorageLocal(root, seed_builtins=False)
    validation = storage.validate_all()
    if validation.get("valid"):
        print("Skills OK")
        return 0
    print("Skills validation failed:")
    for item in validation.get("skills", []):
        if item.get("valid"):
            continue
        print(f"- {item.get('name') or item.get('path')}")
        for error in item.get("errors", []):
            print(f"  {error.get('path')}: {error.get('message')}")
    return 1


def _task_summary(records: list[Any]) -> tuple[int, int, int, int]:
    scheduled = sum(1 for record in records if record.status in {"active", "paused"})
    failed = sum(1 for record in records if record.state == "failed")
    archived = sum(1 for record in records if record.state == "completed")
    return len(records), scheduled, failed, archived


def _print_tasks_summary(config_dir: Path) -> int:
    root = config_dir / BaseAgentConfig.TASKS_DIRNAME
    if not root.exists():
        print(f"Tasks root: {root}")
        print("Tasks: not found")
        return 0

    from ...core.runtime import list_task_records

    records = list_task_records(root, include_archived=True)
    total, scheduled, failed, archived = _task_summary(records)
    print(f"Tasks root: {root}")
    print(f"Total: {total}")
    print(f"Scheduled: {scheduled}")
    print(f"Needs attention: {failed}")
    print(f"Archive: {archived}")
    return 0


def _format_task_record(record: Any) -> str:
    label = record.title or record.content or record.task_id
    if len(label) > 96:
        label = label[:93] + "..."
    channel = record.delivery_channel or "local"
    return (
        f"{record.task_id} [{record.state}] "
        f"{record.run_at.isoformat(sep=' ')} "
        f"{record.task_type or 'task'} via {channel} - {label}"
    )


def _print_tasks_list(config_dir: Path, *, scope: str) -> int:
    root = config_dir / BaseAgentConfig.TASKS_DIRNAME
    if not root.exists():
        print(f"Tasks root: {root}")
        print("No tasks found.")
        return 0

    from ...core.runtime import list_archived_task_records, list_task_records

    current = list_task_records(root)
    if scope == "scheduled":
        records = [record for record in current if record.status in {"active", "paused"}]
    elif scope == "attention":
        records = [record for record in current if record.status == "failed"]
    elif scope == "archive":
        records = list_archived_task_records(root)
    else:
        records = [*current, *list_archived_task_records(root)]
    if not records:
        print("No tasks found.")
        return 0
    for record in records:
        print(_format_task_record(record))
    return 0


def _prompt_message_list_count_terminal_ui(ui: TerminalUI) -> Optional[int]:
    choice = ui.select(
        label="Recent message count",
        subtitle="Choose how many recent stored messages to print.",
        options=[
            MenuOption(str(count), str(count), f"Show the latest {count} stored messages.")
            for count in MESSAGE_LIST_COUNT_CHOICES
        ]
        + [MenuOption("custom", "Custom", "Enter a custom number.")],
        default_index=0,
    )
    if choice is None:
        return None
    if choice.key != "custom":
        return int(choice.key)

    while True:
        raw_value = ui.ask_text(
            "Recent message count",
            default=str(DEFAULT_MESSAGE_LIST_COUNT),
            subtitle="Enter a positive whole number.",
        ).strip()
        if raw_value.isdigit() and int(raw_value) > 0:
            return int(raw_value)
        ui.print_panel("Please enter a positive whole number.", title="Input Required")


def _prompt_memory_list_days_terminal_ui(ui: TerminalUI) -> Optional[int]:
    choice = ui.select(
        label="Recent daily journal days",
        subtitle="Choose how many natural days to scan for daily journals.",
        options=[
            MenuOption(str(days), str(days), f"Show existing daily journals from the last {days} day(s).")
            for days in MEMORY_LIST_DAY_CHOICES
        ]
        + [MenuOption("custom", "Custom", "Enter a custom number of days.")],
        default_index=1,
    )
    if choice is None:
        return None
    if choice.key != "custom":
        return int(choice.key)

    while True:
        raw_value = ui.ask_text(
            "Recent daily journal days",
            default=str(DEFAULT_MEMORY_LIST_DAYS),
            subtitle="Enter a positive whole number.",
        ).strip()
        if raw_value.isdigit() and int(raw_value) > 0:
            return int(raw_value)
        ui.print_panel("Please enter a positive whole number.", title="Input Required")


def _run_inspect_section(
    ui: TerminalUI,
    config_dir: Path,
    title: str,
    actions: Sequence[MenuOption],
    run_action: Callable[[str], Optional[int]],
) -> None:
    while True:
        option = ui.select_menu(
            title=f"xAgent Inspect / {title}",
            subtitle=f"Runtime: {config_dir}",
            options=actions,
            footer="↑/↓ Move • Enter Select  •  q Back",
        )
        if option is None or option.key == "back":
            ui.clear()
            return

        ui.clear()
        exit_code = run_action(option.key)
        if exit_code is None:
            continue
        if exit_code != 0:
            ui.print_panel(f"{title} action exited with status {exit_code}.", title="Inspect")
        ui.pause(f"Press Enter to return to {title}")


def _run_config_inspect_launcher(ui: TerminalUI, config_dir: Path) -> None:
    actions = [
        MenuOption("show", "Show", "Print config.yaml."),
        MenuOption("validate", "Validate", "Parse and validate config.yaml."),
        MenuOption("path", "Path", "Print the config file path."),
        MenuOption("back", "Back", "Return to Inspect."),
    ]
    _run_inspect_section(
        ui,
        config_dir,
        "Config",
        actions,
        lambda key: handle_config(_launcher_args(config_dir=str(config_dir), config_command=key)),
    )


def _run_identity_inspect_launcher(ui: TerminalUI, config_dir: Path) -> None:
    actions = [
        MenuOption("show", "Show", "Print identity.md."),
        MenuOption("path", "Path", "Print the identity file path."),
        MenuOption("back", "Back", "Return to Inspect."),
    ]
    _run_inspect_section(
        ui,
        config_dir,
        "Identity",
        actions,
        lambda key: handle_identity(_launcher_args(config_dir=str(config_dir), identity_command=key)),
    )


def _run_memory_inspect_launcher(ui: TerminalUI, config_dir: Path) -> None:
    from .runtime import handle_memory

    actions = [
        MenuOption("stats", "Stats", "Show memory file counts and bytes."),
        MenuOption("list", "List", "Choose how many recent daily journal days to print."),
        MenuOption("search", "Search", "Search memory markdown files."),
        MenuOption("back", "Back", "Return to Inspect."),
    ]

    def run_action(key: str) -> Optional[int]:
        if key == "stats":
            return handle_memory(
                _launcher_args(config_dir=str(config_dir), memory_command="stats", scope="all", yes=False)
            )
        if key == "list":
            days = _prompt_memory_list_days_terminal_ui(ui)
            if days is None:
                return None
            return handle_memory(
                _launcher_args(config_dir=str(config_dir), memory_command="list", days=days)
            )
        if key == "search":
            query = ui.ask_text("Memory search query").strip()
            if not query:
                return None
            return handle_memory(
                _launcher_args(config_dir=str(config_dir), memory_command="search", query=query, scope="all")
            )
        return None

    _run_inspect_section(ui, config_dir, "Memory", actions, run_action)


def _run_message_inspect_launcher(ui: TerminalUI, config_dir: Path) -> None:
    actions = [
        MenuOption("stats", "Stats", "Show message stream storage stats."),
        MenuOption("list", "List", "Choose how many recent stored messages to print."),
        MenuOption("back", "Back", "Return to Inspect."),
    ]

    def run_action(key: str) -> Optional[int]:
        if key == "stats":
            return handle_messages(_launcher_args(config_dir=str(config_dir), messages_command="stats"))
        count = _prompt_message_list_count_terminal_ui(ui)
        if count is None:
            return None
        return handle_messages(_launcher_args(config_dir=str(config_dir), messages_command="list", count=count, offset=0))

    _run_inspect_section(ui, config_dir, "Message", actions, run_action)


def _run_skills_inspect_launcher(ui: TerminalUI, config_dir: Path) -> None:
    actions = [
        MenuOption("summary", "Summary", "Show skill counts and validation totals."),
        MenuOption("list", "List", "List skill packages."),
        MenuOption("search", "Search", "Search skill files."),
        MenuOption("validate", "Validate", "Validate all skills."),
        MenuOption("back", "Back", "Return to Inspect."),
    ]

    def run_action(key: str) -> Optional[int]:
        if key == "summary":
            return _print_skills_summary(config_dir)
        if key == "list":
            return _print_skills_list(config_dir)
        if key == "search":
            query = ui.ask_text("Skill search query").strip()
            if not query:
                return None
            return _print_skills_search(config_dir, query)
        return _print_skills_validation(config_dir)

    _run_inspect_section(ui, config_dir, "Skills", actions, run_action)


def _run_tasks_inspect_launcher(ui: TerminalUI, config_dir: Path) -> None:
    actions = [
        MenuOption("summary", "Summary", "Show scheduled task counts."),
        MenuOption("scheduled", "Scheduled", "List active and paused tasks."),
        MenuOption("attention", "Needs attention", "List failed tasks."),
        MenuOption("archive", "Archive", "List completed tasks."),
        MenuOption("all", "All", "List every task lifecycle state."),
        MenuOption("back", "Back", "Return to Inspect."),
    ]
    _run_inspect_section(
        ui,
        config_dir,
        "Tasks",
        actions,
        lambda key: _print_tasks_summary(config_dir)
        if key == "summary"
        else _print_tasks_list(config_dir, scope=key),
    )


def _agent_launcher_options(*, has_agents: bool) -> list[MenuOption]:
    return [
        MenuOption("create", "Create Agent", "Create a new managed agent."),
        MenuOption("switch", "Switch Agent", "Choose the active agent.", disabled=not has_agents),
        MenuOption("list", "List Agents", "Show registered agents.", disabled=not has_agents),
        MenuOption("delete", "Delete Agent", "Delete an agent and all of its local data.", disabled=not has_agents),
        MenuOption("back", "Back", "Return to the main launcher."),
    ]


def _agent_selection_options(registry, *, include_back: bool = False) -> list[MenuOption]:
    options = [
        MenuOption(
            key=name,
            title=f"{name} (active)" if name == registry.active_agent else name,
            description=str(entry.path),
        )
        for name, entry in sorted(registry.agents.items())
    ]
    if include_back:
        options.append(MenuOption("back", "Back", "Return to Agents."))
    return options


def _agent_list_text(registry) -> str:
    if not registry.agents:
        return "No agents are registered yet.\nCreate one with: xagent agents create default"
    lines = [f"Active: {registry.active_agent}", ""]
    for name, entry in sorted(registry.agents.items()):
        marker = "active" if name == registry.active_agent else "available"
        lines.append(f"{name} ({marker})")
        lines.append(f"  Path: {entry.path}")
    return "\n".join(lines)


def _agent_directory_has_contents(path: Path) -> bool:
    return path.exists() and path.is_dir() and any(path.iterdir())


def _confirm_agent_directory_delete(ui: TerminalUI, *, name: str, path: Path, verb: str) -> bool:
    confirmed = ui.confirm(
        f"{verb} agent '{name}' and delete all local data at {path}?",
        default=False,
    )
    return confirmed is True


def _run_agent_switch_launcher(ui: TerminalUI, registry) -> None:
    option = ui.select_menu(
        title="xAgent Agents / Switch",
        subtitle=f"Active: {registry.active_agent}",
        options=_agent_selection_options(registry, include_back=True),
        footer="↑/↓ Move • Enter Select  •  q Back",
    )
    if option is None or option.key == "back":
        return
    try:
        select_agent(str(option.key))
        ui.print_panel(f"Active agent: {option.key}", title="Agents", border_style="green")
        raise ReturnToLauncherHome()
    except AgentRegistryError as exc:
        ui.print_panel(f"Cannot select agent: {exc}", title="Agents", border_style="red")
        ui.pause()


def _run_agent_delete_launcher(ui: TerminalUI, registry) -> None:
    option = ui.select_menu(
        title="xAgent Agents / Delete",
        subtitle="Deleting an agent removes its config, identity, memory, messages, workspace, skills, tasks, logs, and run state.",
        options=_agent_selection_options(registry, include_back=True),
        footer="↑/↓ Move • Enter Select  •  q Back",
    )
    if option is None or option.key == "back":
        return
    name = str(option.key)
    entry = registry.agents[name]
    if not _confirm_agent_directory_delete(ui, name=name, path=entry.path, verb="Delete"):
        ui.print_panel("Delete cancelled.", title="Agents")
        ui.pause()
        return
    exit_code = handle_agents(_launcher_args(agents_action="remove", name=name, yes=True))
    if exit_code == 0:
        raise ReturnToLauncherHome()
    ui.pause("Press Enter to return to Agents")


def _run_agent_launcher() -> int:
    ui = TerminalUI()
    try:
        while True:
            try:
                registry = load_agent_registry()
                has_agents = bool(registry.agents)
                subtitle = f"Active: {registry.active_agent}\nRegistry: {BaseAgentConfig.DEFAULT_CONFIG_DIR}/agents.yaml"
            except AgentRegistryError as exc:
                registry = None
                has_agents = False
                subtitle = f"{exc}\nCreate your first agent to initialize the registry."
            option = ui.select_menu(
                title="xAgent Agents",
                subtitle=subtitle,
                options=_agent_launcher_options(has_agents=has_agents),
                footer="↑/↓ Move • Enter Select  •  q Back",
            )
            if option is None or option.key == "back":
                ui.clear()
                return 0
            ui.clear()
            if option.key == "switch":
                if registry is not None:
                    _run_agent_switch_launcher(ui, registry)
                continue
            if option.key == "create":
                name = ui.ask_text(
                    "Agent name",
                    subtitle="Use lowercase letters, digits, hyphens, or underscores.",
                ).strip()
                if not name:
                    continue
                replace_existing = False
                try:
                    candidate_path = default_agent_dir(name)
                    replace_existing = _agent_directory_has_contents(candidate_path)
                    if replace_existing and not _confirm_agent_directory_delete(
                        ui,
                        name=name,
                        path=candidate_path,
                        verb="Replace existing directory for",
                    ):
                        ui.print_panel("Create cancelled.", title="Agents")
                        ui.pause()
                        continue
                except AgentRegistryError as exc:
                    ui.print_panel(f"Cannot create agent: {exc}", title="Agents", border_style="red")
                    ui.pause()
                    continue
                exit_code = handle_agents(
                    _launcher_args(agents_action="create", name=name, title=None, yes=replace_existing)
                )
                if exit_code == 0:
                    try:
                        select_agent(name)
                    except AgentRegistryError:
                        pass
                    raise ReturnToLauncherHome()
                if exit_code == SETUP_EXIT_CANCELLED:
                    # The setup wizard was cancelled; remain in Agents so
                    # the user can retry or choose another action.
                    continue
                ui.pause("Press Enter to return to Agents")
                continue
            if option.key == "delete":
                if registry is not None:
                    _run_agent_delete_launcher(ui, registry)
                continue
            if registry is None:
                ui.print_panel(
                    "No agents are registered yet.\nCreate one with: xagent agents create default",
                    title="Agents",
                )
            else:
                ui.print_panel(_agent_list_text(registry), title="Agents")
            ui.pause("Press Enter to return to Agents")
    except ReturnToLauncherHome:
        ui.clear()
        return 0


def _run_inspect_launcher(config_dir: Path) -> int:
    ui = TerminalUI()
    sections = [
        MenuOption("config", "Config", "Inspect config.yaml."),
        MenuOption("identity", "Identity", "Inspect identity.md."),
        MenuOption("memory", "Memory", "Inspect long-term memory files."),
        MenuOption("message", "Message", "Inspect stored conversation messages."),
        MenuOption("skills", "Skills", "Inspect Agent Skills packages."),
        MenuOption("tasks", "Tasks", "Inspect scheduled task files."),
        MenuOption("back", "Back", "Return to the main launcher."),
    ]

    launchers = {
        "config": _run_config_inspect_launcher,
        "identity": _run_identity_inspect_launcher,
        "memory": _run_memory_inspect_launcher,
        "message": _run_message_inspect_launcher,
        "skills": _run_skills_inspect_launcher,
        "tasks": _run_tasks_inspect_launcher,
    }

    try:
        while True:
            option = ui.select_menu(
                title="xAgent Inspect",
                subtitle=f"Runtime: {config_dir}",
                options=sections,
                footer="↑/↓ Move • Enter Select  •  q Back",
            )
            if option is None or option.key == "back":
                ui.clear()
                return 0

            ui.clear()
            launchers[option.key](ui, config_dir)
    except ReturnToLauncherHome:
        ui.clear()
        return 0


def _run_voice_channel_launcher(ui: TerminalUI, config_dir: Path) -> None:
    while True:
        config = _launcher_config_snapshot(config_dir)
        option = ui.select_menu(
            title="xAgent Channel / Voice",
            subtitle=f"Runtime: {config_dir}",
            options=_voice_channel_options(config),
            footer="↑/↓ Move • Enter Select  •  q Back",
        )
        if option is None or option.key == "back":
            ui.clear()
            return
        ui.clear()
        if option.key == "setup":
            _run_voice_provider_mode_launcher(ui, config_dir, config)
            continue
        if option.key in {"start", "stop", "restart", "status", "logs"}:
            exit_code = _run_managed_channel_action(config_dir, CHANNEL_VOICE, str(option.key))
        else:
            exit_code = handle_voice(
                _launcher_args(
                    config_dir=str(config_dir),
                    user_id="local_voice",
                    verbose=False,
                    list_devices=option.key == "devices",
                    input_device=None,
                    output_device=None,
                    memory=True,
                )
            )
        if exit_code != 0:
            ui.print_panel(f"Voice action exited with status {exit_code}.", title="Channel")
        ui.pause("Press Enter to return to Voice")


def _run_channel_launcher(config_dir: Path) -> int:
    ui = TerminalUI()

    try:
        while True:
            channel_option = ui.select_menu(
                title="xAgent Channel",
                subtitle="Choose how you want to enter or manage the runtime.",
                options=_launcher_channel_options(config_dir),
                footer="↑/↓ Move • Enter Select  •  q Back",
            )
            if channel_option is None or channel_option.key == "back":
                ui.clear()
                return 0

            if channel_option.key == "chat":
                ui.clear()
                exit_code = handle_chat(
                    _launcher_args(
                        message=None,
                        config_dir=str(config_dir),
                        user_id=None,
                        verbose=False,
                        stream=None,
                        events=False,
                        memory=True,
                    )
                )
                if exit_code != 0:
                    ui.print_panel(f"Chat exited with status {exit_code}.", title="Channel")
                ui.pause("Press Enter to return to Channel")
                continue

            if channel_option.key == "voice":
                ui.clear()
                _run_voice_channel_launcher(ui, config_dir)
                continue

            channel_title = getattr(channel_option, "title", str(channel_option.key).title())
            ui.clear()
            _run_managed_channel_launcher(
                ui,
                config_dir,
                str(channel_option.key),
                channel_title=channel_title,
            )
    except ReturnToLauncherHome:
        ui.clear()
        return 0


def _run_web_launcher(config_dir: Path) -> int:
    ui = TerminalUI()

    try:
        while True:
            option = ui.select_menu(
                title="xAgent Web UI",
                subtitle=f"Runtime: {config_dir}",
                options=_web_client_actions(config_dir),
                footer="↑/↓ Move • Enter Select  •  q Back",
            )
            if option is None or option.key == "back":
                ui.clear()
                return 0
            ui.clear()
            exit_code = _run_web_action(config_dir, str(option.key))
            if exit_code != 0:
                ui.print_panel(f"Web client action exited with status {exit_code}.", title="Web UI")
            ui.pause("Press Enter to return to Web UI")
    except ReturnToLauncherHome:
        ui.clear()
        return 0


def _run_interactive_launcher() -> int:
    from .runtime import _runtime_is_initialized, print_quick_start  # noqa: F401

    ui = TerminalUI()

    while True:
        _agent_name, config_dir, has_agents = _active_agent_context()
        overview = build_runtime_overview(config_dir)
        initialized = overview.initialized
        option = ui.select_menu(
            title=f"xAgent {_xagent_version_text()}",
            subtitle=_launcher_overview_subtitle(overview),
            options=_launcher_options(initialized=initialized, has_agents=has_agents),
            footer="↑/↓ Move • Enter Select  •  q Exit",
        )
        if option is None or option.key == "exit":
            ui.clear()
            return 0

        if option.disabled:
            ui.clear()
            message = (
                "This workflow needs a configured runtime first. Choose Agents, then Create Agent."
                if not has_agents
                else "This workflow needs a configured runtime first. Choose Setup to create config.yaml and identity.md."
            )
            ui.print_panel(
                message,
                title="Not Ready",
            )
            ui.pause()
            continue

        ui.clear()
        if option.key == "agent":
            _run_agent_launcher()
            continue
        if option.key == "setup":
            if initialized:
                _run_resetup_launcher(config_dir)
                continue
            exit_code = handle_init(_launcher_args(agent=None, config_dir=None, force=False, schema=False))
            if exit_code == 0:
                continue
        elif option.key == "channel":
            _run_channel_launcher(config_dir)
            continue
        elif option.key == "web":
            _run_web_launcher(config_dir)
            continue
        elif option.key == "inspect":
            _run_inspect_launcher(config_dir)
            continue
        elif option.key == "help":
            ui.print_panel(_launcher_help_content(config_dir=config_dir, initialized=initialized), title="xAgent Help")
        else:
            continue

        ui.pause("Press Enter to return to the launcher")
