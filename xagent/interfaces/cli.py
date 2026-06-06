import argparse
import asyncio
import getpass
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlparse

import yaml

from ..core.providers import (
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
    provider_base_url,
    provider_model_api,
)
from ..core.runtime import create_runtime_heartbeat
from ..utils.image_utils import workspace_blob_relative_path
from ..schemas.attachment import dedupe_attachments
from .base import BaseAgentConfig, BaseAgentRunner
from .channels import (
    CHANNEL_API,
    CHANNEL_FEISHU,
    ChannelSelectionError,
    api_config,
    default_start_channel_from_config,
    feishu_config,
    load_config_file,
    normalize_channel_values,
    voice_config,
)
from .processes import managed_paths, running_pid, start_background, stop_managed_process, tail_text
from .terminal_ui import MenuOption, TerminalUI, rich_terminal_available

from rich.text import Text  # type: ignore[import-not-found]


_WORKSPACE_BLOB_CLI_LINK_RE = re.compile(
    r'(?:https?://[^\s<>"\')\]]+)?/api/workspace/blob\?path=[^\s<>"\')\]]+',
    re.IGNORECASE,
)


def _format_cli_workspace_links(content: Any, workspace_dir: str | Path | None) -> str:
    if content is None:
        return ""
    text = str(content)
    if not text or workspace_dir is None:
        return text

    workspace_root = Path(workspace_dir).expanduser().resolve()

    def local_workspace_path(match: re.Match[str]) -> str:
        source = match.group(0)
        relative_path = workspace_blob_relative_path(source)
        if not relative_path:
            return source
        candidate = (workspace_root / relative_path).resolve()
        if not candidate.is_relative_to(workspace_root):
            return source
        return candidate.as_posix()

    return _WORKSPACE_BLOB_CLI_LINK_RE.sub(local_workspace_path, text)


def _format_cli_attachments(attachments: Any, workspace_dir: str | Path | None) -> str:
    if not isinstance(attachments, list) or workspace_dir is None:
        return ""

    workspace_root = Path(workspace_dir).expanduser().resolve()
    paths: list[str] = []
    for attachment in dedupe_attachments(attachments):
        relative_path = str(attachment.get("path") or "").strip().strip("/")
        if not relative_path:
            relative_path = workspace_blob_relative_path(str(attachment.get("blob_url") or ""))
        if not relative_path:
            continue
        candidate = (workspace_root / relative_path).resolve()
        if not candidate.is_relative_to(workspace_root):
            continue
        paths.append(candidate.as_posix())

    if not paths:
        return ""
    return "\n".join(f"- {path}" for path in paths)

class AgentCLI(BaseAgentRunner):
    """CLI Agent for xAgent."""

    def __init__(
        self,
        config_dir: Optional[str] = None,
        verbose: bool = False,
    ):
        self.verbose = verbose

        if not verbose:
            logging.getLogger().setLevel(logging.CRITICAL)
            logging.getLogger("xagent").setLevel(logging.CRITICAL)
            import warnings

            warnings.filterwarnings("ignore")
        else:
            logging.getLogger().setLevel(logging.INFO)
            logging.getLogger("xagent").setLevel(logging.INFO)

        super().__init__(config_dir=config_dir)

    async def chat_interactive(
        self,
        user_id: Optional[str] = None,
        stream: Optional[bool] = None,
        memory: bool = True,
    ):
        if stream is None:
            stream = not (logging.getLogger().level <= logging.INFO)

        verbose_mode = logging.getLogger().level <= logging.INFO
        user_id = user_id or f"cli_user_{uuid.uuid4().hex[:8]}"
        await self._chat_interactive_terminal_ui(
            user_id=user_id,
            stream=stream,
            memory=memory,
            verbose_mode=verbose_mode,
        )

    async def _chat_interactive_terminal_ui(
        self,
        *,
        user_id: str,
        stream: bool,
        memory: bool,
        verbose_mode: bool,
    ) -> None:
        ui = TerminalUI()
        self._print_terminal_banner(
            ui,
            stream=stream,
            memory=memory,
            verbose_mode=verbose_mode,
        )

        while True:
            try:
                user_input = ui.input("[cyan]You:[/cyan] ").strip()

                if user_input.lower() in ["exit", "quit", "bye"]:
                    ui.print_panel(
                        "Thank you for using xAgent CLI. See you next time.",
                        title="Session Ended",
                    )
                    break

                if user_input.lower() == "clear":
                    await self.message_storage.clear_messages()
                    ui.print_panel(
                        "Global message stream cleared.",
                        title="Cleared",
                    )
                    continue

                if user_input.lower().startswith("stream "):
                    stream_cmd = user_input.lower().split()
                    if len(stream_cmd) == 2 and stream_cmd[1] in {"on", "off"}:
                        stream = stream_cmd[1] == "on"
                        ui.print_panel(
                            f"Streaming {'enabled' if stream else 'disabled'}.",
                            title="Chat Status",
                        )
                    else:
                        ui.print_panel("Usage: stream on/off", title="Chat Status")
                    continue

                if user_input.lower().startswith("memory "):
                    memory_cmd = user_input.lower().split()
                    if len(memory_cmd) == 2 and memory_cmd[1] in {"on", "off"}:
                        memory = memory_cmd[1] == "on"
                        ui.print_panel(
                            f"Memory {'enabled' if memory else 'disabled'}.",
                            title="Chat Status",
                        )
                    else:
                        ui.print_panel("Usage: memory on/off", title="Chat Status")
                    continue

                if user_input.lower() == "help":
                    self._show_terminal_help(ui)
                    continue

                if not user_input:
                    ui.print_panel("Enter a message to chat with the agent.", title="Empty Input")
                    continue

                if not hasattr(self.agent, "chat_events"):
                    response = await self.agent(
                        user_message=user_input,
                        user_id=user_id,
                        enable_memory=memory,
                    )
                    ui.print_panel(
                        self._format_cli_output(response),
                        title="xAgent",
                        border_style="green",
                    )
                    continue

                await self._print_chat_events_terminal_ui(
                    ui=ui,
                    user_message=user_input,
                    user_id=user_id,
                    enable_memory=memory,
                    stream=stream,
                )

            except KeyboardInterrupt:
                ui.print_panel(
                    "Session interrupted by user.",
                    title="Session Ended",
                )
                break
            except Exception as exc:
                ui.print_panel(f"An error occurred: {exc}", title="Error", border_style="red")
                if verbose_mode:
                    import traceback

                    traceback.print_exc()

    async def chat_single(
        self,
        message: str,
        user_id: Optional[str] = None,
        memory: bool = True,
    ):
        user_id = user_id or f"cli_user_{uuid.uuid4().hex[:8]}"
        response = await self.agent(
            user_message=message,
            user_id=user_id,
            enable_memory=memory,
        )
        return self._format_cli_output(response) if isinstance(response, str) else response

    def _format_cli_output(self, content: Any) -> str:
        return _format_cli_workspace_links(content, getattr(self, "workspace_dir", None))

    def _format_cli_event_attachments(self, attachments: Any) -> str:
        return _format_cli_attachments(attachments, getattr(self, "workspace_dir", None))

    async def print_single_chat_events(
        self,
        message: str,
        user_id: Optional[str] = None,
        stream: bool = False,
        memory: bool = True,
    ) -> None:
        user_id = user_id or f"cli_user_{uuid.uuid4().hex[:8]}"
        await self._print_chat_events(
            user_message=message,
            user_id=user_id,
            stream=stream,
            enable_memory=memory,
        )

    async def _print_chat_events(
        self,
        *,
        user_message: str,
        user_id: str,
        stream: bool,
        enable_memory: bool,
    ) -> None:
        line_open = False
        line_has_streamed_text = False
        async for event in self.agent.chat_events(
            user_message=user_message,
            user_id=user_id,
            stream=stream,
            enable_memory=enable_memory,
        ):
            event_type = event.get("type")
            if event_type == "message_start":
                if line_open:
                    print()
                print("🤖 Agent: ", end="", flush=True)
                line_open = True
                line_has_streamed_text = False
                continue
            if event_type == "message_delta":
                if not line_open:
                    print("🤖 Agent: ", end="", flush=True)
                    line_open = True
                    line_has_streamed_text = False
                delta = self._format_cli_output(event.get("delta", ""))
                if delta:
                    print(delta, end="", flush=True)
                    line_has_streamed_text = True
                continue
            if event_type == "message_done":
                attachments_text = self._format_cli_event_attachments(event.get("attachments"))
                if not line_open:
                    print("🤖 Agent: ", end="", flush=True)
                    line_open = True
                if not line_has_streamed_text:
                    content = event.get("content", "")
                    if content:
                        print(self._format_cli_output(content), end="", flush=True)
                if line_open:
                    print()
                    line_open = False
                if attachments_text:
                    print(attachments_text)
                line_has_streamed_text = False
                continue
            if event_type == "error":
                if line_open:
                    print()
                    line_open = False
                print(f"❌ {event.get('error', 'Agent processing error.')}")
                continue

        if line_open:
            print()

    async def _print_chat_events_terminal_ui(
        self,
        *,
        ui: TerminalUI,
        user_message: str,
        user_id: str,
        stream: bool,
        enable_memory: bool,
    ) -> None:
        console = ui.console
        line_open = False
        line_has_streamed_text = False

        async for event in self.agent.chat_events(
            user_message=user_message,
            user_id=user_id,
            stream=stream,
            enable_memory=enable_memory,
        ):
            event_type = event.get("type")
            if event_type == "message_start":
                if line_open and console is not None:
                    console.print()
                if console is not None:
                    console.print("[magenta]xAgent[/magenta]: ", end="")
                line_open = True
                line_has_streamed_text = False
                continue
            if event_type == "message_delta":
                if not line_open and console is not None:
                    console.print("[magenta]xAgent[/magenta]: ", end="")
                    line_open = True
                    line_has_streamed_text = False
                delta = self._format_cli_output(event.get("delta", ""))
                if delta and console is not None:
                    console.print(delta, end="", markup=False, highlight=False, soft_wrap=True)
                    line_has_streamed_text = True
                continue
            if event_type == "message_done":
                attachments_text = self._format_cli_event_attachments(event.get("attachments"))
                content = self._format_cli_output(event.get("content", ""))
                if line_has_streamed_text:
                    if console is not None:
                        console.print()
                elif content:
                    ui.print_panel(content, title="xAgent", border_style="green")
                else:
                    ui.print_panel("", title="xAgent", border_style="green")
                if attachments_text:
                    ui.print_panel(attachments_text, title="Attachments")
                line_open = False
                line_has_streamed_text = False
                continue
            if event_type == "error":
                ui.print_panel(event.get("error", "Agent processing error."), title="Error", border_style="red")
                line_open = False
                line_has_streamed_text = False

        if line_open and console is not None:
            console.print()

    def _print_terminal_banner(
        self,
        ui: TerminalUI,
        *,
        stream: bool,
        memory: bool,
        verbose_mode: bool,
    ) -> None:
        config_msg = (
            f"Config: {self.config_path}"
            if self.config_path.is_file()
            else f"Config: default values ({self.config_path} not found)"
        )
        ui.print_panel(
            "\n".join([
                config_msg,
                f"Runtime: {self.config_dir}",
                f"Model: {self.agent.model}",
                f"Tools: {len(self.agent.tools)} loaded",
                (
                    "Status: "
                    f"verbose={'on' if verbose_mode else 'off'}, "
                    f"stream={'on' if stream else 'off'}, "
                    f"memory={'on' if memory else 'off'}"
                ),
                "",
                "Type a message to chat. Use help for commands or exit to leave.",
            ]),
            title="xAgent Chat",
        )

    def _show_terminal_help(self, ui: TerminalUI) -> None:
        tool_lines = [f"- {tool_name}" for tool_name in self.agent.tools.keys()] or ["- No built-in tools available"]
        ui.print_panel(
            "\n".join([
                "Chat commands:",
                "exit, quit, bye    Exit the chat session",
                "clear              Clear the agent message stream",
                "stream on/off      Toggle streamed delta printing",
                "memory on/off      Toggle memory storage mode",
                "help               Show this help message",
                "",
                "Built-in tools:",
                *tool_lines,
            ]),
            title="Chat Help",
        )


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


@dataclass(frozen=True)
class FeishuInitSelection:
    """Interactive choices used to configure the Feishu channel."""

    app_id: str
    app_secret: str
    stream: bool = False
    enable_memory: bool = True
    group_history_count: int = 10
    show_sender_ids: bool = False
    group_reply_without_mention: bool = False
    credential_mode: str = "one_click"


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
NON_OPENAI_IMAGE_GENERATION_PROVIDERS = (
    "none",
    "openai",
    "minimax",
)
OPENAI_IMAGE_GENERATION_PROVIDERS = (
    "openai",
    "none",
)
MINIMAX_IMAGE_GENERATION_PROVIDERS = (
    "minimax",
    "none",
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


def _native_image_generation_provider(provider: str) -> str:
    if provider == PROVIDER_OPENAI:
        return "openai"
    if provider == PROVIDER_MINIMAX:
        return "minimax"
    if provider == PROVIDER_QWEN:
        return "qwen"
    return "none"


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
        search_provider="openai",
        image_generation_provider=_native_image_generation_provider(PROVIDER_OPENAI),
    )


def _weather_output_schema() -> dict:
    return {
        "class_name": "WeatherReport",
        "fields": {
            "location": {
                "type": "str",
                "description": "Location name",
            },
            "temperature_celsius": {
                "type": "int",
                "description": "Temperature in degrees Celsius",
            },
            "condition": {
                "type": "str",
                "description": "Short weather condition summary",
            },
        },
    }


def _config_yaml(selection: InitSelection, schema: bool = False) -> str:
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
        "channels": {
            "api": {
                "host": BaseAgentConfig.DEFAULT_HOST,
                "port": BaseAgentConfig.DEFAULT_PORT,
            }
        },
    }
    if selection.voice_enabled:
        voice_provider = selection.voice_provider or "soniox"
        voice_config = {
            "provider": voice_provider,
            "enable_interruptions": False,
            "audio": {
                "input": "auto",
                "output": "auto",
            },
            "wake": {
                "enabled": False,
                "wake_phrases": ["xAgent"],
                "exit_phrases": ["exit", "stop", "goodbye", "that's all", "never mind"],
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
        if search_config["provider"] != selection.provider:
            search_config["api_key"] = (
                selection.search_api_key.strip() or _search_api_key_placeholder(search_config["provider"])
            )
        elif selection.search_api_key:
            search_config["api_key"] = selection.search_api_key
    config["search"] = search_config
    selected_image_generation_provider = selection.image_generation_provider or "none"
    image_generation_config = {"provider": selected_image_generation_provider}
    if selected_image_generation_provider == "openai" and selection.provider != PROVIDER_OPENAI:
        image_generation_config["api_key"] = (
            selection.image_generation_api_key.strip() or OPENAI_IMAGE_API_KEY_PLACEHOLDER
        )
    elif selected_image_generation_provider == "minimax" and selection.provider != PROVIDER_MINIMAX:
        image_generation_config["api_key"] = (
            selection.image_generation_api_key.strip() or MINIMAX_IMAGE_API_KEY_PLACEHOLDER
        )
    elif selected_image_generation_provider == "qwen" and selection.provider != PROVIDER_QWEN:
        image_generation_config["api_key"] = (
            selection.image_generation_api_key.strip() or QWEN_IMAGE_API_KEY_PLACEHOLDER
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
    if schema:
        config["output_schema"] = _weather_output_schema()
    return yaml.safe_dump(config, sort_keys=False, allow_unicode=False)


def _default_identity_markdown() -> str:
    return """# Identity

You are a helpful assistant.
Answer clearly, keep responses practical, and adapt to the user's language.
Be concise by default, and add detail when it improves the answer.
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
    if provider == PROVIDER_OPENAI:
        return _native_image_generation_provider(provider)
    if provider == PROVIDER_MINIMAX:
        return _native_image_generation_provider(provider)
    if provider == PROVIDER_QWEN:
        return _native_image_generation_provider(provider)
    return _select_option(
        "Image generation provider",
        NON_OPENAI_IMAGE_GENERATION_PROVIDERS,
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

    if provider in {PROVIDER_OPENAI, PROVIDER_MINIMAX, PROVIDER_QWEN}:
        image_generation_provider = _native_image_generation_provider(provider)
    else:
        image_generation_provider = _terminal_select_option(
            wizard_ui,
            "Image Generation Provider",
            NON_OPENAI_IMAGE_GENERATION_PROVIDERS,
            descriptions={
                "none": "Do not enable image generation.",
                "openai": "Use OpenAI image generation.",
                "minimax": "Use MiniMax image generation.",
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
    )


def collect_init_selection(
    *,
    input_func: Callable[[str], str] = input,
    secret_input_func: Callable[[str], str] = getpass.getpass,
) -> InitSelection:
    """Collect init choices from the terminal before writing files."""
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
    )


def init_agent_directory(
    config_dir: Optional[str] = None,
    *,
    force: bool = False,
    schema: bool = False,
    selection: Optional[InitSelection] = None,
    clear_runtime_data: bool = False,
) -> InitResult:
    """Create config.yaml, identity.md, and runtime directories."""
    resolved_dir = Path(config_dir or BaseAgentConfig.DEFAULT_CONFIG_DIR).expanduser().resolve()
    resolved_dir.mkdir(parents=True, exist_ok=True)
    config_path = resolved_dir / BaseAgentConfig.CONFIG_FILENAME
    identity_path = resolved_dir / BaseAgentConfig.IDENTITY_FILENAME
    memory_dir = resolved_dir / BaseAgentConfig.MEMORY_DIRNAME
    messages_dir = resolved_dir / BaseAgentConfig.MESSAGE_DIRNAME
    workspace_dir = resolved_dir / BaseAgentConfig.WORKSPACE_DIRNAME
    skills_dir = resolved_dir / BaseAgentConfig.SKILLS_DIRNAME
    tasks_dir = resolved_dir / BaseAgentConfig.TASKS_DIRNAME
    managed_paths = (config_path, identity_path)
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
            config_path=config_path,
            identity_path=identity_path,
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
    config_path.write_text(_config_yaml(selection, schema=schema), encoding="utf-8")
    identity_path.write_text(selection.identity, encoding="utf-8")

    TerminalUI().print_panel(
        "\n".join([
            "xAgent project files written successfully.",
            f"Config: {config_path}",
            f"Identity: {identity_path}",
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
        config_path=config_path,
        identity_path=identity_path,
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


def _format_init_command(command: str, *, config_dir: Path) -> str:
    default_dir = Path(BaseAgentConfig.DEFAULT_CONFIG_DIR).expanduser().resolve()
    if config_dir == default_dir:
        return command
    return f"{command} --dir {shlex.quote(str(config_dir))}"


def _print_init_next_steps(*, config_dir: Path, selection: InitSelection) -> None:
    ready_now = [
        (
            "chat",
            _format_init_command("xagent chat", config_dir=config_dir),
            "Talk to the agent in your terminal.",
        ),
        (
            "web",
            _format_init_command("xagent web", config_dir=config_dir),
            "Open the built-in Web UI.",
        ),
        (
            "api",
            _format_init_command("xagent service start api", config_dir=config_dir),
            "Run the HTTP / SSE / WebSocket service in the background.",
        ),
    ]
    if selection.voice_enabled:
        ready_now.insert(
            2,
            (
                "voice",
                _format_init_command("xagent voice", config_dir=config_dir),
                "Talk to the agent by microphone.",
            ),
        )

    feishu_init = _format_init_command("xagent init feishu", config_dir=config_dir)
    feishu_start = _format_init_command("xagent service start feishu", config_dir=config_dir)
    
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
    content.append(f"\n        Create a Feishu bot config, then start it with ")
    content.append(feishu_start, style="cyan")
    content.append(".")
    
    TerminalUI().print_panel(content, title="Next Steps")


def _feishu_routing_label(group_reply_without_mention: bool) -> str:
    if group_reply_without_mention:
        return "Direct chats + every group/topic message"
    return "Direct chats + group/topic @mentions"


def _feishu_delivery_label(stream: bool) -> str:
    if stream:
        return "Streaming cards"
    return "Standard segmented messages"


def _feishu_memory_label(enable_memory: bool) -> str:
    if enable_memory:
        return "Enabled"
    return "Disabled"


def _feishu_group_history_label(group_history_count: int) -> str:
    if group_history_count <= 0:
        return "Disabled"
    unit = "message" if group_history_count == 1 else "messages"
    return f"{group_history_count} recent {unit}"


def _feishu_sender_label(*, group_history_count: int, show_sender_ids: bool) -> str:
    if group_history_count <= 0:
        if show_sender_ids:
            return "Ready once room context is enabled"
        return "Not used while room context is off"
    if show_sender_ids:
        return "Display names + sender IDs"
    return "Display names only"


def _add_dir_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dir",
        dest="config_dir",
        default=None,
        help="Directory containing config.yaml and identity.md (default: ~/.xagent)",
    )


def _add_channel_argument(
    parser: argparse.ArgumentParser,
    *,
    default_label: str,
) -> None:
    parser.add_argument(
        "--channel",
        dest="channels",
        action="append",
        default=None,
        metavar="CHANNELS",
        help=f"Channel(s) to use: api, feishu, all, or comma-separated values (default: {default_label})",
    )


def _add_service_channel_argument(parser: argparse.ArgumentParser, *, default_label: str) -> None:
    parser.add_argument(
        "channels",
        nargs="?",
        default=None,
        metavar="channel",
        choices=(CHANNEL_API, CHANNEL_FEISHU, "all"),
        help=f"Channel to manage: api, feishu, or all (default: {default_label})",
    )


def _add_api_runtime_arguments(
    parser: argparse.ArgumentParser,
    *,
    open_by_default: bool = False,
) -> None:
    parser.add_argument("--host", default=None, help="API host override")
    parser.add_argument("--port", type=int, default=None, help="API port override")
    if open_by_default:
        parser.add_argument(
            "--open",
            action=argparse.BooleanOptionalAction,
            default=True,
            dest="open_browser",
            help="Open the API web UI",
        )
    else:
        parser.add_argument("--open", action="store_true", dest="open_browser", help="Open the API web UI")
    parser.add_argument(
        "--max-concurrent-chats",
        type=int,
        default=None,
        help="Maximum concurrent chat/observe requests",
    )
    parser.add_argument(
        "--queue-timeout",
        type=float,
        default=None,
        help="Seconds to wait for a chat slot",
    )
    parser.add_argument(
        "--chat-timeout",
        type=float,
        default=None,
        help="Seconds before a chat or observe request times out",
    )


def _hide_subparser_choice(subparsers: argparse._SubParsersAction, name: str) -> None:
    subparsers._choices_actions = [
        action for action in subparsers._choices_actions if action.dest != name
    ]


class XAgentArgumentParser(argparse.ArgumentParser):
    """Root parser with task-oriented help instead of argparse's flat command list."""

    def error(self, message: str) -> None:
        if self.prog == "xagent" and "invalid choice" in message:
            self.print_usage(sys.stderr)
            self.exit(2, "xagent: error: unknown command. Use 'xagent --help' to see available commands.\n")
        super().error(message)

    def format_help(self) -> str:
        if self.prog != "xagent":
            return super().format_help()
        return "\n".join([
            "usage: xagent <command> ...",
            "",
            "xAgent command line interface",
            "",
            "Start here:",
            "  init      Create config.yaml and identity.md",
            "  chat      Chat with the configured agent",
            "  voice     Talk with the configured agent by microphone",
            "  web       Open the built-in web UI",
            "",
            "Runtime:",
            "  observe   Ingest context without generating a reply",
            "  service   Manage background channels",
            "  doctor    Check local xAgent readiness",
            "",
            "Advanced:",
            "  inspect   Inspect configuration, identity, memory, or messages",
            "  version   Show xAgent version",
            "",
            "Examples:",
            "  xagent init",
            "  xagent chat \"Help me plan today\"",
            "  xagent voice",
            "  xagent web",
            "  xagent service start api",
            "  xagent service logs feishu -f",
            "",
            "Use 'xagent <command> --help' for command-specific help.",
            "",
        ])


def build_parser() -> argparse.ArgumentParser:
    parser = XAgentArgumentParser(
        prog="xagent",
        description="xAgent command line interface",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    init_parser = subparsers.add_parser("init", help="Create config.yaml and identity.md")
    _add_dir_argument(init_parser)
    init_parser.add_argument("--force", action="store_true", help="Overwrite init-managed files")
    init_parser.add_argument("--schema", action="store_true", help="Include a starter output_schema example")
    init_parser.set_defaults(handler=handle_init)

    init_sub = init_parser.add_subparsers(dest="init_target", metavar="[target]")
    init_feishu = init_sub.add_parser("feishu", help="Enable and configure the Feishu channel")
    _add_dir_argument(init_feishu)
    init_feishu.add_argument("--app-id", dest="app_id", default=None, help="Feishu app id (cli_xxx)")
    init_feishu.add_argument("--app-secret", dest="app_secret", default=None, help="Feishu app secret")
    init_feishu.add_argument(
        "--manual",
        action="store_true",
        help="Enter App ID/Secret manually instead of the one-click QR code flow",
    )
    init_feishu.add_argument(
        "--stream",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Use Feishu streaming cards for in-progress replies",
    )
    init_feishu.add_argument(
        "--memory",
        dest="enable_memory",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable long-term memory for Feishu chats",
    )
    init_feishu.add_argument(
        "--group-history-count",
        type=int,
        default=None,
        help="How many recent group/topic messages to fetch before replying (default: 10)",
    )
    init_feishu.add_argument(
        "--show-sender-ids",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Include Feishu sender IDs in fetched room context",
    )
    init_feishu.add_argument(
        "--group-reply-without-mention",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Route every group/topic message, even without an @mention",
    )
    init_feishu.add_argument("--force", action="store_true", help="Overwrite existing channels.feishu config")
    init_feishu.set_defaults(handler=handle_init_feishu)

    chat_parser = subparsers.add_parser("chat", help="Chat with the configured agent")
    chat_parser.add_argument("message", nargs="?", help="Single message to send; omit for interactive chat")
    _add_dir_argument(chat_parser)
    chat_parser.add_argument("--user-id", dest="user_id", default=None, help="Speaker identifier")
    chat_parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    chat_parser.add_argument(
        "--events",
        action="store_true",
        help="Use segmented event output for a single message",
    )
    chat_parser.add_argument(
        "--stream",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Print message deltas as they are emitted in event mode",
    )
    chat_parser.add_argument(
        "--memory",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable or disable memory tools",
    )
    chat_parser.set_defaults(handler=handle_chat)

    voice_parser = subparsers.add_parser("voice", help="Talk with the configured agent by microphone")
    _add_dir_argument(voice_parser)
    voice_parser.add_argument("--user-id", dest="user_id", default="local_voice", help="Speaker identifier")
    voice_parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    voice_parser.add_argument(
        "--list-devices",
        action="store_true",
        help="List available local audio input/output devices and exit",
    )
    voice_parser.add_argument(
        "--input-device",
        default=None,
        help="Override voice input device by name, #index, index, or auto",
    )
    voice_parser.add_argument(
        "--output-device",
        default=None,
        help="Override voice output device by name, #index, index, or auto",
    )
    voice_parser.add_argument(
        "--memory",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable or disable memory tools",
    )
    voice_parser.set_defaults(handler=handle_voice)

    web_parser = subparsers.add_parser("web", help="Open the built-in web UI")
    _add_dir_argument(web_parser)
    _add_api_runtime_arguments(web_parser, open_by_default=True)
    web_parser.set_defaults(handler=handle_web)

    observe_parser = subparsers.add_parser("observe", help="Ingest context without generating a reply")
    observe_parser.add_argument("text", help="Observation text to store")
    _add_dir_argument(observe_parser)
    observe_parser.add_argument("--source", default="cli", help="Observation source label")
    observe_parser.add_argument("--event-type", default="observation", help="Observation event type")
    observe_parser.add_argument("--metadata", default=None, help="JSON object with observation metadata")
    observe_parser.set_defaults(handler=handle_observe)

    service_parser = subparsers.add_parser("service", help="Manage background channels")
    service_sub = service_parser.add_subparsers(dest="service_command", metavar="<action>")
    service_sub.required = True

    service_start = service_sub.add_parser("start", help="Start a background channel")
    _add_dir_argument(service_start)
    _add_service_channel_argument(service_start, default_label="auto")
    _add_api_runtime_arguments(service_start)
    service_start.set_defaults(handler=handle_start)

    service_stop = service_sub.add_parser("stop", help="Stop background channels")
    _add_dir_argument(service_stop)
    _add_service_channel_argument(service_stop, default_label="all")
    service_stop.set_defaults(handler=handle_stop)

    service_restart = service_sub.add_parser("restart", help="Restart background channels")
    _add_dir_argument(service_restart)
    _add_service_channel_argument(service_restart, default_label="all")
    _add_api_runtime_arguments(service_restart)
    service_restart.set_defaults(handler=handle_restart)

    service_status = service_sub.add_parser("status", help="Show background channel status")
    _add_dir_argument(service_status)
    _add_service_channel_argument(service_status, default_label="all")
    service_status.add_argument("--json", action="store_true", dest="json_output", help="Print machine-readable JSON")
    service_status.set_defaults(handler=handle_status)

    service_logs = service_sub.add_parser("logs", help="Show background channel logs")
    _add_dir_argument(service_logs)
    _add_service_channel_argument(service_logs, default_label="all")
    service_logs.add_argument("--lines", type=int, default=80, help="Number of trailing log lines to print")
    service_logs.add_argument("--follow", "-f", action="store_true", help="Follow log output")
    service_logs.set_defaults(handler=handle_logs)

    doctor_parser = subparsers.add_parser("doctor", help="Check local xAgent readiness")
    _add_dir_argument(doctor_parser)
    _add_channel_argument(doctor_parser, default_label="all")
    doctor_parser.add_argument("--online", action="store_true", help="Include network/model checks")
    doctor_parser.set_defaults(handler=handle_doctor)

    inspect_parser = subparsers.add_parser("inspect", help="Inspect configuration, identity, memory, or messages")
    inspect_sub = inspect_parser.add_subparsers(dest="inspect_target", metavar="<target>")
    inspect_sub.required = True

    config_parser = inspect_sub.add_parser("config", help="Show or validate config.yaml")
    config_sub = config_parser.add_subparsers(dest="config_command", metavar="<subcommand>")
    config_sub.required = True
    for command_name in ("show", "validate", "path"):
        config_cmd = config_sub.add_parser(command_name, help=f"{command_name} config.yaml")
        _add_dir_argument(config_cmd)
        config_cmd.set_defaults(handler=handle_config)

    identity_parser = inspect_sub.add_parser("identity", help="Show identity.md information")
    identity_sub = identity_parser.add_subparsers(dest="identity_command", metavar="<subcommand>")
    identity_sub.required = True
    for command_name in ("show", "path"):
        identity_cmd = identity_sub.add_parser(command_name, help=f"{command_name} identity.md")
        _add_dir_argument(identity_cmd)
        identity_cmd.set_defaults(handler=handle_identity)

    memory_parser = inspect_sub.add_parser("memory", help="Inspect or clear long-term memory files")
    memory_sub = memory_parser.add_subparsers(dest="memory_command", metavar="<subcommand>")
    memory_sub.required = True
    for command_name in ("stats", "list", "clear"):
        memory_cmd = memory_sub.add_parser(command_name, help=f"{command_name} memory")
        _add_dir_argument(memory_cmd)
        memory_cmd.add_argument("--scope", default="all", choices=("daily", "weekly", "monthly", "yearly", "all"))
        memory_cmd.add_argument("--yes", action="store_true", help="Confirm destructive operations")
        memory_cmd.set_defaults(handler=handle_memory)
    memory_show = memory_sub.add_parser("show", help="Show a memory markdown file by relative path")
    _add_dir_argument(memory_show)
    memory_show.add_argument("path", help="Relative path inside memory/")
    memory_show.set_defaults(handler=handle_memory)
    memory_search = memory_sub.add_parser("search", help="Search memory markdown files")
    _add_dir_argument(memory_search)
    memory_search.add_argument("query", help="Search query")
    memory_search.add_argument("--scope", default="all", choices=("daily", "weekly", "monthly", "yearly", "all"))
    memory_search.set_defaults(handler=handle_memory)

    messages_parser = inspect_sub.add_parser("messages", help="Inspect or clear the message stream")
    messages_sub = messages_parser.add_subparsers(dest="messages_command", metavar="<subcommand>")
    messages_sub.required = True
    messages_stats = messages_sub.add_parser("stats", help="Show message stream statistics")
    _add_dir_argument(messages_stats)
    messages_stats.set_defaults(handler=handle_messages)
    messages_list = messages_sub.add_parser("list", help="List recent messages")
    _add_dir_argument(messages_list)
    messages_list.add_argument("--count", type=int, default=20, help="Number of recent messages")
    messages_list.add_argument("--offset", type=int, default=0, help="Number of recent messages to skip")
    messages_list.set_defaults(handler=handle_messages)
    messages_clear = messages_sub.add_parser("clear", help="Clear all stored messages")
    _add_dir_argument(messages_clear)
    messages_clear.add_argument("--yes", action="store_true", help="Confirm clearing the message stream")
    messages_clear.set_defaults(handler=handle_messages)

    version_parser = subparsers.add_parser("version", help="Show xAgent version")
    version_parser.set_defaults(handler=handle_version)

    internal_run = subparsers.add_parser("_run-channel", help=argparse.SUPPRESS)
    internal_run.add_argument("channel", choices=(CHANNEL_API, CHANNEL_FEISHU))
    _add_dir_argument(internal_run)
    _add_api_runtime_arguments(internal_run)
    internal_run.set_defaults(handler=handle_run_channel_internal)
    _hide_subparser_choice(subparsers, "_run-channel")

    return parser


def handle_init(args: argparse.Namespace) -> int:
    resolved_dir = Path(args.config_dir or BaseAgentConfig.DEFAULT_CONFIG_DIR).expanduser().resolve()
    conflicts = tuple(
        path for path in (
            resolved_dir / BaseAgentConfig.CONFIG_FILENAME,
            resolved_dir / BaseAgentConfig.IDENTITY_FILENAME,
        )
        if path.exists()
    )
    if conflicts and not args.force:
        result = init_agent_directory(
            args.config_dir,
            force=args.force,
            schema=args.schema,
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
        args.config_dir,
        force=args.force,
        schema=args.schema,
        selection=selection,
        clear_runtime_data=clear_runtime_data,
    )
    if result.wrote_files:
        _print_init_next_steps(config_dir=result.config_path.parent, selection=selection)
    return 0 if result.wrote_files else 1


def handle_chat(args: argparse.Namespace) -> int:
    agent_cli = AgentCLI(config_dir=args.config_dir, verbose=args.verbose)

    if args.message is None:
        async def run_interactive_chat():
            await agent_cli.chat_interactive(
                user_id=args.user_id,
                stream=args.stream,
                memory=args.memory,
            )

        asyncio.run(run_interactive_chat())
        return 0

    event_mode = bool(args.events or args.stream is not None or hasattr(agent_cli.agent, "chat_events"))
    stream = bool(args.stream) if args.stream is not None else False

    async def run_single_message():
        if event_mode and hasattr(agent_cli.agent, "chat_events"):
            await agent_cli.print_single_chat_events(
                message=args.message,
                user_id=args.user_id,
                stream=stream,
                memory=args.memory,
            )
            return

        response = await agent_cli.chat_single(
            message=args.message,
            user_id=args.user_id,
            memory=args.memory,
        )
        print(response)

    asyncio.run(run_single_message())
    return 0


def handle_voice(args: argparse.Namespace) -> int:
    if getattr(args, "verbose", False):
        logging.getLogger().setLevel(logging.INFO)
        logging.getLogger("xagent").setLevel(logging.INFO)
    else:
        logging.getLogger().setLevel(logging.CRITICAL)
        logging.getLogger("xagent").setLevel(logging.CRITICAL)

    try:
        if getattr(args, "list_devices", False):
            from ..voice.audio import list_audio_devices_text

            print(list_audio_devices_text())
            return 0

        runner = BaseAgentRunner(config_dir=args.config_dir)
        from ..voice.config import VoiceChannelConfig
        from ..voice.factory import create_local_voice_runtime
        from ..voice.runtime import VoiceRuntimeOptions

        runtime_config = VoiceChannelConfig.from_dict(voice_config(runner.config))
        runtime = create_local_voice_runtime(
            agent=runner.agent,
            config=runtime_config,
            options=VoiceRuntimeOptions(
                user_id=args.user_id or "local_voice",
                enable_memory=bool(args.memory),
                stream=True,
                tasks_dir=getattr(runner, "tasks_dir", None),
            ),
            input_device=getattr(args, "input_device", None),
            output_device=getattr(args, "output_device", None),
        )
    except Exception as exc:
        print(f"Failed to start voice channel: {exc}")
        return 1

    try:
        asyncio.run(runtime.run_forever())
    except KeyboardInterrupt:
        print("\nVoice channel stopped.")
    except Exception as exc:
        print(f"Voice channel error: {exc}")
        return 1
    return 0


def handle_server(args: argparse.Namespace) -> int:
    from .server import AgentHTTPServer

    server_kwargs = {
        "config_dir": args.config_dir,
        "enable_web": not args.no_web,
    }
    if args.max_concurrent_chats is not None:
        server_kwargs["max_concurrent_chats"] = args.max_concurrent_chats
    if args.queue_timeout is not None:
        server_kwargs["chat_queue_timeout"] = args.queue_timeout
    if args.chat_timeout is not None:
        server_kwargs["chat_timeout"] = args.chat_timeout

    server = AgentHTTPServer(**server_kwargs)
    server.run(host=args.host, port=args.port, open_browser=args.open_browser)
    return 0


def handle_web(args: argparse.Namespace) -> int:
    """Run the API channel in the foreground and open the built-in web UI."""
    try:
        config = _load_runtime_config(args)
    except ChannelSelectionError as exc:
        return _handle_channel_error(exc)
    return _run_api_channel(args, config)


def _runtime_dir(args: argparse.Namespace) -> Path:
    raw_dir = getattr(args, "config_dir", None) or BaseAgentConfig.DEFAULT_CONFIG_DIR
    return Path(raw_dir).expanduser().resolve()


def _config_path(args: argparse.Namespace) -> Path:
    return _runtime_dir(args) / BaseAgentConfig.CONFIG_FILENAME


def _identity_path(args: argparse.Namespace) -> Path:
    return _runtime_dir(args) / BaseAgentConfig.IDENTITY_FILENAME


def _load_runtime_config(args: argparse.Namespace) -> dict[str, Any]:
    return load_config_file(_runtime_dir(args))


def _channel_arg_values(args: argparse.Namespace) -> Optional[list[str]]:
    values = getattr(args, "channels", None)
    if values is None:
        return None
    if isinstance(values, str):
        return [values]
    return list(values)


def _select_channels(args: argparse.Namespace, *, default: str) -> tuple[list[str], dict[str, Any]]:
    config = _load_runtime_config(args)
    values = _channel_arg_values(args)
    if values is None and default == "auto":
        channels = [default_start_channel_from_config(config)]
    else:
        channels = normalize_channel_values(values, default=default, config=config)
    return channels, config


def _handle_channel_error(exc: ChannelSelectionError) -> int:
    print(f"Error: {exc}")
    return 1


def _channel_command(channel: str, args: argparse.Namespace) -> list[str]:
    command = [sys.executable, "-m", "xagent.interfaces.cli", "_run-channel", channel]
    config_dir = getattr(args, "config_dir", None)
    if config_dir:
        command.extend(["--dir", config_dir])

    for flag, attr in (
        ("--host", "host"),
        ("--port", "port"),
        ("--max-concurrent-chats", "max_concurrent_chats"),
        ("--queue-timeout", "queue_timeout"),
        ("--chat-timeout", "chat_timeout"),
    ):
        value = getattr(args, attr, None)
        if value is not None:
            command.extend([flag, str(value)])
    if getattr(args, "open_browser", False):
        command.append("--open")
    return command


def _api_runtime_values(
    args: argparse.Namespace,
    config: dict[str, Any],
) -> tuple[dict[str, Any], Optional[str], Optional[int], bool]:
    api_cfg = api_config(config)
    server_kwargs: dict[str, Any] = {
        "config_dir": getattr(args, "config_dir", None),
    }

    runtime_mapping = (
        ("max_concurrent_chats", "max_concurrent_chats"),
        ("queue_timeout", "chat_queue_timeout"),
        ("chat_timeout", "chat_timeout"),
    )
    for args_attr, server_key in runtime_mapping:
        value = getattr(args, args_attr, None)
        if value is None:
            value = api_cfg.get(args_attr)
        if value is not None:
            server_kwargs[server_key] = value

    host = getattr(args, "host", None) or api_cfg.get("host")
    port = getattr(args, "port", None)
    if port is None:
        port = api_cfg.get("port")
    open_browser = bool(getattr(args, "open_browser", False))
    return server_kwargs, host, port, open_browser


def _run_api_channel(args: argparse.Namespace, config: dict[str, Any]) -> int:
    from .server import AgentHTTPServer

    server_kwargs, host, port, open_browser = _api_runtime_values(args, config)
    server = AgentHTTPServer(**server_kwargs)
    print(f"xAgent api channel ready (model={server.agent.model}).")
    server.run(host=host, port=port, open_browser=open_browser)
    return 0


def _run_feishu_channel(args: argparse.Namespace, config: dict[str, Any]) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        from ..integrations.feishu import FeishuAdapter, FeishuAdapterConfig
    except ImportError as exc:  # pragma: no cover - defensive
        print(f"Failed to import Feishu adapter: {exc}")
        return 1

    feishu_data = feishu_config(config)
    if not feishu_data:
        print("Feishu channel is not configured. Run: xagent init feishu")
        return 1

    try:
        feishu_runtime_config = FeishuAdapterConfig.from_dict(feishu_data)
    except Exception as exc:
        print(f"Invalid Feishu channel config: {exc}")
        return 1

    runner = BaseAgentRunner(config_dir=getattr(args, "config_dir", None))
    adapter = FeishuAdapter(agent=runner.agent, config=feishu_runtime_config)

    async def _run_daemon() -> bool:
        heartbeat = create_runtime_heartbeat(
            runner.agent,
            config.get("runtime") if isinstance(config, dict) else None,
            logger_=logging.getLogger(__name__),
        )
        stop_requested = False
        loop = asyncio.get_running_loop()
        old_handlers: dict[int, object] = {}
        signal_handlers: list[int] = []

        def _request_stop() -> None:
            nonlocal stop_requested
            stop_requested = True
            adapter._stop_event.set()
            adapter._safe_stop()

        def _handle_stop(_signum: int, _frame) -> None:
            loop.call_soon_threadsafe(_request_stop)

        for signum in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
            if signum is None:
                continue
            try:
                loop.add_signal_handler(signum, _request_stop)
                signal_handlers.append(signum)
            except (NotImplementedError, RuntimeError):
                old_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, _handle_stop)

        try:
            if heartbeat is not None:
                await heartbeat.start()
            await adapter.run()
        finally:
            for signum in signal_handlers:
                try:
                    loop.remove_signal_handler(signum)
                except (NotImplementedError, RuntimeError):
                    pass
            for signum, previous_handler in old_handlers.items():
                signal.signal(signum, previous_handler)
            if heartbeat is not None:
                await heartbeat.stop()
        return stop_requested

    print(f"xAgent Feishu channel ready (model={runner.agent.model}).")
    print(f"Connecting to Feishu (app_id={feishu_runtime_config.app_id})...")
    try:
        stop_requested = asyncio.run(_run_daemon())
    except KeyboardInterrupt:
        stop_requested = True
    except RuntimeError as exc:
        print(f"{exc}")
        return 1

    if stop_requested:
        print("Feishu channel stopped.")
    return 0


def _run_channel(channel: str, args: argparse.Namespace, config: dict[str, Any]) -> int:
    if channel == CHANNEL_API:
        return _run_api_channel(args, config)
    if channel == CHANNEL_FEISHU:
        return _run_feishu_channel(args, config)
    print(f"Unknown channel: {channel}")
    return 1


def handle_run_channel_internal(args: argparse.Namespace) -> int:
    try:
        config = _load_runtime_config(args)
    except ChannelSelectionError as exc:
        return _handle_channel_error(exc)
    return _run_channel(args.channel, args, config)


def handle_run(args: argparse.Namespace) -> int:
    try:
        channels, config = _select_channels(args, default="auto")
    except ChannelSelectionError as exc:
        return _handle_channel_error(exc)

    if len(channels) == 1:
        return _run_channel(channels[0], args, config)

    processes: list[subprocess.Popen] = []
    try:
        for channel in channels:
            print(f"Starting {channel} channel in foreground...")
            process = subprocess.Popen(_channel_command(channel, args))
            processes.append(process)
        while processes:
            for process in list(processes):
                return_code = process.poll()
                if return_code is not None:
                    processes.remove(process)
                    if return_code != 0:
                        return return_code
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("Stopping foreground channels...")
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        return 0
    return 0


def _start_background_channels(args: argparse.Namespace, channels: list[str]) -> int:
    ok = True
    config_dir = _runtime_dir(args)
    for channel in channels:
        paths = managed_paths(config_dir, channel)
        result = start_background(
            _channel_command(channel, args),
            pid_path=paths.pid_path,
            log_path=paths.log_path,
        )
        if result.ok:
            print(f"Started {channel} channel in background (pid={result.pid}).")
            print(f"Logs: {paths.log_path}")
            continue

        ok = False
        print(f"Failed to start {channel} channel: {result.error}")
        if result.recent_output:
            print(result.recent_output)
    return 0 if ok else 1


def handle_start(args: argparse.Namespace) -> int:
    try:
        channels, _config = _select_channels(args, default="auto")
    except ChannelSelectionError as exc:
        return _handle_channel_error(exc)

    return _start_background_channels(args, channels)


def handle_stop(args: argparse.Namespace) -> int:
    try:
        channels, _config = _select_channels(args, default="all")
    except ChannelSelectionError as exc:
        return _handle_channel_error(exc)

    ok = True
    config_dir = _runtime_dir(args)
    for channel in channels:
        paths = managed_paths(config_dir, channel)
        stopped, message = stop_managed_process(paths.pid_path)
        ok = ok and stopped
        print(f"{channel}: {message}")
    return 0 if ok else 1


def handle_restart(args: argparse.Namespace) -> int:
    try:
        channels, _config = _select_channels(args, default="all")
    except ChannelSelectionError as exc:
        return _handle_channel_error(exc)

    restart_values = dict(vars(args))
    restart_values["channels"] = channels
    restart_args = argparse.Namespace(**restart_values)
    stop_code = handle_stop(restart_args)
    start_code = _start_background_channels(restart_args, channels)
    return 0 if stop_code == 0 and start_code == 0 else 1


def handle_status(args: argparse.Namespace) -> int:
    try:
        channels, _config = _select_channels(args, default="all")
    except ChannelSelectionError as exc:
        return _handle_channel_error(exc)

    config_dir = _runtime_dir(args)
    rows: list[dict[str, Any]] = []
    for channel in channels:
        paths = managed_paths(config_dir, channel)
        pid = running_pid(paths.pid_path)
        rows.append({
            "channel": channel,
            "status": "running" if pid is not None else "stopped",
            "pid": pid,
            "pid_path": str(paths.pid_path),
            "log_path": str(paths.log_path),
        })

    if getattr(args, "json_output", False):
        print(json.dumps({"channels": rows}, indent=2, sort_keys=True))
        return 0

    for row in rows:
        pid_text = f" pid={row['pid']}" if row["pid"] is not None else ""
        print(f"{row['channel']}: {row['status']}{pid_text}")
        print(f"  pid: {row['pid_path']}")
        print(f"  log: {row['log_path']}")
    return 0


def _follow_log(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8", errors="replace") as handle:
        handle.seek(0, os.SEEK_END)
        while True:
            line = handle.readline()
            if line:
                print(line, end="")
                continue
            time.sleep(0.2)


def handle_logs(args: argparse.Namespace) -> int:
    if getattr(args, "follow", False):
        raw_channels = _channel_arg_values(args)
        explicit_tokens = [
            token.strip().lower()
            for raw_channel in (raw_channels or [])
            for token in str(raw_channel).split(",")
            if token.strip()
        ]
        if len(explicit_tokens) != 1 or explicit_tokens[0] not in {CHANNEL_API, CHANNEL_FEISHU}:
            print("--follow requires an explicit single channel")
            return 1

    try:
        channels, _config = _select_channels(args, default="all")
    except ChannelSelectionError as exc:
        return _handle_channel_error(exc)

    if getattr(args, "follow", False) and len(channels) != 1:
        print("--follow requires exactly one channel")
        return 1

    config_dir = _runtime_dir(args)
    for index, channel in enumerate(channels):
        paths = managed_paths(config_dir, channel)
        if len(channels) > 1:
            if index:
                print("")
            print(f"==> {channel}: {paths.log_path} <==")
        output = tail_text(paths.log_path, max_lines=max(1, int(args.lines)))
        if output:
            print(output)
        elif not paths.log_path.exists():
            print(f"No log file: {paths.log_path}")

    if getattr(args, "follow", False):
        _follow_log(managed_paths(config_dir, channels[0]).log_path)
    return 0


def _feishu_channel_config(selection: FeishuInitSelection) -> dict[str, Any]:
    config: dict[str, Any] = {
        "app_id": selection.app_id,
        "app_secret": selection.app_secret,
        "stream": selection.stream,
        "enable_memory": selection.enable_memory,
        "group_history_count": selection.group_history_count,
        "group_reply_without_mention": selection.group_reply_without_mention,
    }
    if selection.show_sender_ids:
        config["show_sender_ids"] = True
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
            ],
            default_index=0,
        )
        if choice is None:
            raise KeyboardInterrupt()
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

    group_reply_arg = getattr(args, "group_reply_without_mention", None)
    if group_reply_arg is None:
        if interactive:
            choice = wizard_ui.select(
                label="Group Routing",
                subtitle="Choose when the bot should respond in group chats.",
                options=[
                    MenuOption(
                        "mentions",
                        "Direct chats + group @mentions",
                        "Safe default. The bot stays quiet in group until someone mentions it.",
                    ),
                    MenuOption(
                        "ambient",
                        "Direct chats + every group message",
                        "Use for room-assistant scenarios. Higher cost, replies to almost every message, and can feel noisy in busy groups.",
                    ),
                ],
                default_index=0,
            )
            if choice is None:
                raise KeyboardInterrupt()
            group_reply_without_mention = choice.key == "ambient"
        else:
            group_reply_without_mention = False
    else:
        group_reply_without_mention = bool(group_reply_arg)
        if interactive:
            wizard_ui.record("Group Routing", _feishu_routing_label(group_reply_without_mention))

    stream_arg = getattr(args, "stream", None)
    stream = bool(stream_arg) if stream_arg is not None else False

    enable_memory_arg = getattr(args, "enable_memory", None)
    enable_memory = bool(enable_memory_arg) if enable_memory_arg is not None else True

    group_history_arg = getattr(args, "group_history_count", None)
    if group_history_arg is not None and group_history_arg < 0:
        if interactive:
            wizard_ui.print_panel("--group-history-count must be >= 0", title="Feishu Setup Stopped")
        else:
            print("--group-history-count must be >= 0")
        return None
    group_history_count = int(group_history_arg) if group_history_arg is not None else 10

    show_sender_ids_arg = getattr(args, "show_sender_ids", None)
    show_sender_ids = bool(show_sender_ids_arg) if show_sender_ids_arg is not None else False

    selection = FeishuInitSelection(
        app_id=app_id,
        app_secret=app_secret,
        stream=stream,
        enable_memory=enable_memory,
        group_history_count=group_history_count,
        show_sender_ids=show_sender_ids,
        group_reply_without_mention=group_reply_without_mention,
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
    """Try to print ASCII QR code. Returns True if succeeded, False if qrcode unavailable."""
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


def _print_feishu_post_setup(config_path: Path, selection: FeishuInitSelection) -> None:
    config_dir = config_path.parent
    ui = TerminalUI()

    summary = Text()
    summary.append(f"Feishu channel updated in {config_path}\n\n")
    summary.append("Configured behavior:\n")
    summary.append(f"- App ID: {selection.app_id}\n")
    summary.append(f"- Routing: {_feishu_routing_label(selection.group_reply_without_mention)}\n")
    ui.print_panel(summary, title="Feishu Ready", leading_blank_line=True)

    feishu_start = _format_init_command("xagent service start feishu", config_dir=config_dir)
    start_all = _format_init_command("xagent service start all", config_dir=config_dir)
    status = _format_init_command("xagent service status feishu", config_dir=config_dir)
    logs = _format_init_command("xagent service logs feishu -f", config_dir=config_dir)

    next_steps = Text()
    next_steps.append("Run next:\n")
    next_steps.append("start   ")
    next_steps.append(feishu_start, style="cyan")
    next_steps.append("\n        Start only the Feishu channel.\n")
    next_steps.append("all     ")
    next_steps.append(start_all, style="cyan")
    next_steps.append("\n        Start every configured channel for this runtime.\n")
    next_steps.append("status  ")
    next_steps.append(status, style="cyan")
    next_steps.append("\n        Check PID, logs, and whether the bot is already running.\n")
    next_steps.append("logs    ")
    next_steps.append(logs, style="cyan")
    next_steps.append("\n        Follow the Feishu channel log live.\n")

    next_steps.append("\nOptional before group rollout:\n")
    next_steps.append("- im:message.group_msg\n")
    next_steps.append("- im:message.group_at_msg.include_bot:readonly\n")
    next_steps.append("- im:resource:readonly\n")
    next_steps.append("- contact:user.base:readonly\n")
    next_steps.append("- admin:app.info:readonly\n")
    next_steps.append("\nIf you only need direct chats right now, you can skip the group permission work and start the bot immediately.")
    if selection.group_reply_without_mention:
        next_steps.append("\n\n")
        next_steps.append(
            "Caution: The bot will respond to most group messages, which may increase costs and create noise in busy rooms.",
            style="yellow",
        )

    ui.print_panel(next_steps, title="Next Steps")

def _register_feishu_app_via_qr() -> Optional[Tuple[str, str]]:
    """Run the one-click Feishu app registration (RFC 8628 device flow).

    Returns ``(app_id, app_secret)`` on success, or ``None`` if the user
    cancelled or the SDK is missing.
    """
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

        if not url:
            print("\nFeishu returned an authorization step, but no browser link was included.")
            print("Please retry `xagent init feishu`, or use `--manual` if the problem persists.")
            print("\nWaiting for authorization... (press Ctrl+C to cancel)\n")
            return

        # Display link
        print("\n🔗 Click this link to authorize (or paste into your browser):\n")
        print(f"{url}\n")
        # if user_code:
        #     print(f"Verification code: {user_code}")
        # if expiry_label:
        #     print(f"Link expires in: {expiry_label}")

        # Try to display ASCII QR code
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
        print("\nThe authorization request expired. Rerun `xagent init feishu` to try again.")
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


def handle_init_feishu(args: argparse.Namespace) -> int:
    ui = TerminalUI()
    config_path = _config_path(args)
    init_command = _format_init_command("xagent init", config_dir=config_path.parent)
    if not config_path.is_file():
        ui.print_panel(
            f"Config not found: {config_path}\nRun {init_command} first, then return to Feishu setup.",
            title="Feishu Setup Stopped",
        )
        return 1

    try:
        with config_path.open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        ui.print_panel(f"Invalid YAML in {config_path}: {exc}", title="Feishu Setup Stopped", border_style="red")
        return 1
    if not isinstance(config, dict):
        ui.print_panel(f"Configuration must be a mapping: {config_path}", title="Feishu Setup Stopped", border_style="red")
        return 1

    channels_cfg = config.setdefault("channels", {})
    if not isinstance(channels_cfg, dict):
        ui.print_panel("channels must be a dictionary", title="Feishu Setup Stopped", border_style="red")
        return 1
    if "feishu" in channels_cfg and not args.force:
        force_command = _format_init_command("xagent init feishu --force", config_dir=config_path.parent)
        ui.print_panel(
            f"channels.feishu already exists in {config_path}.\nRun {force_command} to overwrite the Feishu channel settings.",
            title="Feishu Setup Stopped",
        )
        return 1

    intro_lines = [
        f"Runtime: {config_path.parent}",
        f"Config: {config_path}",
    ]
    if "feishu" in channels_cfg:
        intro_lines.append("Existing channels.feishu settings will be replaced.")
    ui.print_panel("\n".join(intro_lines), title="Feishu Setup", leading_blank_line=True)

    try:
        selection = collect_feishu_init_selection_terminal_ui(args=args, ui=ui)
    except KeyboardInterrupt:
        ui.print_panel("Feishu setup cancelled before writing config.", title="Feishu Setup Cancelled")
        return 1
    if selection is None:
        return 1

    api_cfg = channels_cfg.setdefault("api", {})
    if isinstance(api_cfg, dict):
        api_cfg.setdefault("host", BaseAgentConfig.DEFAULT_HOST)
        api_cfg.setdefault("port", BaseAgentConfig.DEFAULT_PORT)

    channels_cfg["feishu"] = _feishu_channel_config(selection)

    config_path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=False), encoding="utf-8")
    _print_feishu_post_setup(config_path, selection)
    return 0


def handle_observe(args: argparse.Namespace) -> int:
    metadata = None
    if args.metadata:
        try:
            metadata = json.loads(args.metadata)
        except json.JSONDecodeError as exc:
            print(f"Invalid metadata JSON: {exc}")
            return 1
        if not isinstance(metadata, dict):
            print("--metadata must be a JSON object")
            return 1

    runner = BaseAgentRunner(config_dir=args.config_dir)

    async def _run_observe():
        result = await runner.agent.observe(
            context=args.text,
            source=args.source,
            event_type=args.event_type,
            metadata=metadata,
        )
        if hasattr(result, "model_dump"):
            print(json.dumps(result.model_dump(), indent=2, sort_keys=True))
        else:
            print(result)

    asyncio.run(_run_observe())
    return 0


def handle_config(args: argparse.Namespace) -> int:
    path = _config_path(args)
    if args.config_command == "path":
        print(path)
        return 0
    if args.config_command == "show":
        if not path.is_file():
            print(f"Config not found: {path}")
            return 1
        print(path.read_text(encoding="utf-8"), end="")
        return 0
    if args.config_command == "validate":
        BaseAgentRunner(config_dir=args.config_dir)
        print(f"Config OK: {path}")
        return 0
    print(f"Unknown config command: {args.config_command}")
    return 1


def handle_identity(args: argparse.Namespace) -> int:
    path = _identity_path(args)
    if args.identity_command == "path":
        print(path)
        return 0
    if args.identity_command == "show":
        if not path.is_file():
            print(f"Identity not found: {path}")
            return 1
        print(path.read_text(encoding="utf-8"), end="")
        return 0
    print(f"Unknown identity command: {args.identity_command}")
    return 1


def _memory_root(args: argparse.Namespace) -> Path:
    return _runtime_dir(args) / BaseAgentConfig.MEMORY_DIRNAME


def _memory_scope_root(args: argparse.Namespace) -> Path:
    scope = getattr(args, "scope", "all")
    root = _memory_root(args)
    return root if scope == "all" else root / scope


def _safe_memory_path(args: argparse.Namespace, relative_path: str) -> Optional[Path]:
    root = _memory_root(args).resolve()
    requested = (root / relative_path).resolve()
    if not requested.is_relative_to(root):
        return None
    return requested


def handle_memory(args: argparse.Namespace) -> int:
    root = _memory_root(args)
    scope_root = _memory_scope_root(args)

    if args.memory_command == "stats":
        files = sorted(scope_root.rglob("*.md")) if scope_root.exists() else []
        total_bytes = sum(path.stat().st_size for path in files if path.is_file())
        print(f"Memory root: {root}")
        print(f"Scope: {getattr(args, 'scope', 'all')}")
        print(f"Files: {len(files)}")
        print(f"Bytes: {total_bytes}")
        return 0

    if args.memory_command == "list":
        files = sorted(path for path in scope_root.rglob("*.md") if path.is_file()) if scope_root.exists() else []
        for path in files:
            print(path.relative_to(root))
        return 0

    if args.memory_command == "show":
        path = _safe_memory_path(args, args.path)
        if path is None:
            print("Access denied: memory path escapes memory root")
            return 1
        if not path.is_file() or path.suffix != ".md":
            print(f"Memory file not found: {path}")
            return 1
        print(path.read_text(encoding="utf-8", errors="replace"), end="")
        return 0

    if args.memory_command == "search":
        if not scope_root.exists():
            return 0
        needle = args.query.casefold()
        for path in sorted(scope_root.rglob("*.md")):
            if not path.is_file():
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            for line_number, line in enumerate(lines, 1):
                if needle in line.casefold():
                    print(f"{path.relative_to(root)}:{line_number}:{line}")
        return 0

    if args.memory_command == "clear":
        if not getattr(args, "yes", False):
            print("Refusing to clear memory without --yes")
            return 1
        target = scope_root
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        print(f"Cleared memory scope: {getattr(args, 'scope', 'all')}")
        return 0

    print(f"Unknown memory command: {args.memory_command}")
    return 1


def handle_messages(args: argparse.Namespace) -> int:
    runner = BaseAgentRunner(config_dir=args.config_dir)
    storage = runner.message_storage

    async def _run_messages() -> int:
        if args.messages_command == "stats":
            total = await storage.get_message_count()
            info = storage.get_stream_info() if hasattr(storage, "get_stream_info") else {}
            print(json.dumps({"total": total, "storage": info}, indent=2, sort_keys=True))
            return 0

        if args.messages_command == "list":
            messages = await storage.get_messages(count=args.count, offset=args.offset)
            payload = []
            for message in messages:
                item = message.model_dump(mode="json") if hasattr(message, "model_dump") else str(message)
                payload.append(item)
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0

        if args.messages_command == "clear":
            if not getattr(args, "yes", False):
                print("Refusing to clear messages without --yes")
                return 1
            await storage.clear_messages()
            print("Cleared message stream")
            return 0

        print(f"Unknown messages command: {args.messages_command}")
        return 1

    return asyncio.run(_run_messages())


def handle_doctor(args: argparse.Namespace) -> int:
    config_dir = _runtime_dir(args)
    config_path = config_dir / BaseAgentConfig.CONFIG_FILENAME
    identity_path = config_dir / BaseAgentConfig.IDENTITY_FILENAME
    ok = True

    print(f"Runtime dir: {config_dir}")
    if config_path.is_file():
        print(f"Config: ok ({config_path})")
    else:
        print(f"Config: missing ({config_path})")
        ok = False

    if identity_path.is_file() and identity_path.read_text(encoding="utf-8").strip():
        print(f"Identity: ok ({identity_path})")
    else:
        print(f"Identity: missing or empty ({identity_path})")
        ok = False

    try:
        config = load_config_file(config_dir)
        channels = normalize_channel_values(getattr(args, "channels", None), default="all", config=config)
    except ChannelSelectionError as exc:
        print(f"Channels: {exc}")
        return 1

    print(f"Channels: {', '.join(channels)}")
    if CHANNEL_FEISHU in channels:
        data = feishu_config(config)
        if data.get("app_id") and data.get("app_secret"):
            print("Feishu: configured")
        else:
            print("Feishu: missing app_id/app_secret")
            ok = False
    if args.online:
        print("Online checks are not implemented yet.")
    return 0 if ok else 1


def handle_version(_args: argparse.Namespace) -> int:
    try:
        from xagent.__version__ import __version__
    except Exception:  # pragma: no cover - defensive
        __version__ = "unknown"
    print(f"xAgent {__version__}")
    print(f"Python {sys.version.split()[0]}")
    return 0


def _runtime_is_initialized(config_dir: Path) -> bool:
    config_path = config_dir / BaseAgentConfig.CONFIG_FILENAME
    identity_path = config_dir / BaseAgentConfig.IDENTITY_FILENAME
    if not config_path.is_file() or not identity_path.is_file():
        return False
    try:
        return bool(identity_path.read_text(encoding="utf-8").strip())
    except OSError:
        return False


def print_quick_start() -> None:
    config_dir = Path(BaseAgentConfig.DEFAULT_CONFIG_DIR).expanduser().resolve()
    initialized = _runtime_is_initialized(config_dir)

    print("xAgent")
    print(f"Runtime dir: {config_dir}")
    print("")
    if not initialized:
        print("Quick start:")
        print("  xagent init")
        print("")
        print("After setup:")
        print("  xagent chat")
        print("  xagent web")
        print("  xagent service")
    else:
        print("Quick start:")
        print("  xagent chat")
        print("  xagent web")
        print("  xagent service")
    print("")
    print("Use 'xagent --help' to see all commands.")


def _xagent_version_text() -> str:
    try:
        from xagent.__version__ import __version__
    except Exception:  # pragma: no cover - defensive
        return "unknown"
    return __version__


def _launcher_args(**kwargs: Any) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def _launcher_options(*, initialized: bool) -> list[MenuOption]:
    setup_title = "Resetup" if initialized else "Setup"
    setup_description = (
        "Re-run setup with --force; runtime data is kept unless you choose to clear it."
        if initialized
        else "Create config, identity, workspace, memory, and tasks."
    )
    return [
        MenuOption(
            key="init",
            title=setup_title,
            description=setup_description,
        ),
        MenuOption(
            key="chat",
            title="Chat",
            description="Talk with the configured agent in the terminal.",
            disabled=not initialized,
        ),
        MenuOption(
            key="web",
            title="Web UI",
            description="Start the built-in browser workspace.",
            disabled=not initialized,
        ),
        MenuOption(
            key="voice",
            title="Voice",
            description="Start microphone mode with the configured runtime.",
            disabled=not initialized,
        ),
        MenuOption(
            key="service",
            title="Services",
            description="Start, stop, inspect, and tail background channels.",
            disabled=not initialized,
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


def _launcher_channel_options(*, include_all: bool = True) -> list[MenuOption]:
    options = [
        MenuOption(
            key=CHANNEL_API,
            title="API",
            description="HTTP, SSE, WebSocket, and browser UI runtime.",
        ),
        MenuOption(
            key=CHANNEL_FEISHU,
            title="Feishu",
            description="Feishu bot channel using the configured app credentials.",
        ),
    ]
    if include_all:
        options.append(
            MenuOption(
                key="all",
                title="All",
                description="Use all enabled channels.",
            )
        )
    options.append(MenuOption(key="back", title="Back", description="Return to the previous menu."))
    return options


def _launcher_help_content(*, config_dir: Path, initialized: bool) -> Text:
    setup_command = "xagent init --force" if initialized else "xagent init"
    content = Text()
    content.append(f"Runtime: {config_dir}\n\n")
    content.append("Common commands:\n")
    content.append("setup    ")
    content.append(_format_init_command(setup_command, config_dir=config_dir), style="cyan")
    content.append("\n         Create or reconfigure config.yaml and identity.md.\n")
    content.append("chat     ")
    content.append(_format_init_command('xagent chat "Help me plan today"', config_dir=config_dir), style="cyan")
    content.append("\n         Send one message, or run ")
    content.append(_format_init_command("xagent chat", config_dir=config_dir), style="cyan")
    content.append(" for an interactive terminal chat.\n")
    content.append("web      ")
    content.append(_format_init_command("xagent web", config_dir=config_dir), style="cyan")
    content.append("\n         Start the API channel in the foreground and open the Web UI.\n")
    content.append("voice    ")
    content.append(_format_init_command("xagent voice", config_dir=config_dir), style="cyan")
    content.append("\n         Start local microphone/speaker mode when voice is configured.\n")
    content.append("service  ")
    content.append(_format_init_command("xagent service start api", config_dir=config_dir), style="cyan")
    content.append("\n         Run API, Feishu, or all enabled channels in the background.\n")
    content.append("status   ")
    content.append(_format_init_command("xagent service status all", config_dir=config_dir), style="cyan")
    content.append("\n         Show PID files and log paths for managed services.\n")
    content.append("logs     ")
    content.append(_format_init_command("xagent service logs feishu -f", config_dir=config_dir), style="cyan")
    content.append("\n         Follow a single channel log; omit -f to print recent lines.\n")
    content.append("feishu   ")
    content.append(_format_init_command("xagent init feishu", config_dir=config_dir), style="cyan")
    content.append("\n         Configure the Feishu bot after base setup.\n")
    content.append("inspect  ")
    content.append(_format_init_command("xagent inspect config show", config_dir=config_dir), style="cyan")
    content.append("\n         Read config, identity, memory, and message state.\n")
    return content


def _run_service_launcher(config_dir: Path) -> int:
    ui = TerminalUI()
    actions = [
        MenuOption("start", "Start", "Start one channel or all enabled channels."),
        MenuOption("stop", "Stop", "Stop one channel or all enabled channels."),
        MenuOption("restart", "Restart", "Restart one channel or all enabled channels."),
        MenuOption("status", "Status", "Show pid and log paths for running services."),
        MenuOption("logs", "Logs", "Print the latest log output for a channel."),
        MenuOption("back", "Back", "Return to the main launcher."),
    ]

    while True:
        option = ui.select_menu(
            title="xAgent Services",
            subtitle=f"Runtime: {config_dir}",
            options=actions,
            footer="↑/↓ Move • Enter Select  •  q Back",
        )
        if option is None or option.key == "back":
            ui.clear()
            return 0

        channel_option = ui.select_menu(
            title="Choose Channel",
            subtitle="Select which runtime slice to manage.",
            options=_launcher_channel_options(),
            footer="↑/↓ Move • Enter Select  •  q Back",
        )
        if channel_option is None or channel_option.key == "back":
            continue
        ui.clear()
        channels = [channel_option.key]
        if option.key == "start":
            exit_code = handle_start(
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
        elif option.key == "stop":
            exit_code = handle_stop(_launcher_args(config_dir=str(config_dir), channels=channels))
        elif option.key == "restart":
            exit_code = handle_restart(
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
        elif option.key == "status":
            exit_code = handle_status(
                _launcher_args(
                    config_dir=str(config_dir),
                    channels=channels,
                    json_output=False,
                )
            )
        else:
            exit_code = handle_logs(
                _launcher_args(
                    config_dir=str(config_dir),
                    channels=channels,
                    lines=80,
                    follow=False,
                )
            )

        if exit_code != 0:
            ui.print_panel(f"Service action exited with status {exit_code}.", title="Services")
        ui.pause("Press Enter to return to Services")


def _print_skills_summary(config_dir: Path) -> int:
    root = config_dir / BaseAgentConfig.SKILLS_DIRNAME
    if not root.exists():
        print(f"Skills root: {root}")
        print("Skills: not found")
        return 0

    from ..components.skills import SkillsStorageLocal

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

    from ..components.skills import SkillsStorageLocal

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

    from ..components.skills import SkillsStorageLocal

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

    from ..components.skills import SkillsStorageLocal

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


def _task_summary(records: list[Any]) -> tuple[int, int, int]:
    active = sum(1 for record in records if record.status == "active")
    failed = sum(1 for record in records if record.state == "failed")
    return len(records), active, failed


def _print_tasks_summary(config_dir: Path) -> int:
    root = config_dir / BaseAgentConfig.TASKS_DIRNAME
    if not root.exists():
        print(f"Tasks root: {root}")
        print("Tasks: not found")
        return 0

    from ..core.runtime import list_task_records

    records = list_task_records(root)
    total, active, failed = _task_summary(records)
    print(f"Tasks root: {root}")
    print(f"Total: {total}")
    print(f"Active: {active}")
    print(f"Failed: {failed}")
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


def _print_tasks_list(config_dir: Path, *, include_failed: bool) -> int:
    root = config_dir / BaseAgentConfig.TASKS_DIRNAME
    if not root.exists():
        print(f"Tasks root: {root}")
        print("No tasks found.")
        return 0

    from ..core.runtime import list_task_records

    records = list_task_records(root, include_failed=include_failed)
    if not records:
        print("No tasks found.")
        return 0
    for record in records:
        print(_format_task_record(record))
    return 0


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
    actions = [
        MenuOption("stats", "Stats", "Show memory file counts and bytes."),
        MenuOption("list", "List", "List memory markdown files."),
        MenuOption("search", "Search", "Search memory markdown files."),
        MenuOption("show", "Show File", "Print one memory file by relative path."),
        MenuOption("back", "Back", "Return to Inspect."),
    ]

    def run_action(key: str) -> Optional[int]:
        if key == "stats":
            return handle_memory(
                _launcher_args(config_dir=str(config_dir), memory_command="stats", scope="all", yes=False)
            )
        if key == "list":
            return handle_memory(
                _launcher_args(config_dir=str(config_dir), memory_command="list", scope="all", yes=False)
            )
        if key == "search":
            query = ui.ask_text("Memory search query").strip()
            if not query:
                return None
            return handle_memory(
                _launcher_args(config_dir=str(config_dir), memory_command="search", query=query, scope="all")
            )
        path = ui.ask_text("Memory file path", subtitle="Use a relative path under memory/.").strip()
        if not path:
            return None
        return handle_memory(_launcher_args(config_dir=str(config_dir), memory_command="show", path=path, scope="all"))

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
        MenuOption("active", "Active", "List pending active tasks."),
        MenuOption("all", "All", "List active and failed task files."),
        MenuOption("back", "Back", "Return to Inspect."),
    ]
    _run_inspect_section(
        ui,
        config_dir,
        "Tasks",
        actions,
        lambda key: _print_tasks_summary(config_dir)
        if key == "summary"
        else _print_tasks_list(config_dir, include_failed=key == "all"),
    )


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


def _run_interactive_launcher() -> int:
    config_dir = Path(BaseAgentConfig.DEFAULT_CONFIG_DIR).expanduser().resolve()
    ui = TerminalUI()

    while True:
        initialized = _runtime_is_initialized(config_dir)
        option = ui.select_menu(
            title=f"xAgent {_xagent_version_text()}",
            subtitle=(
                f"Runtime: {config_dir}\n"
                f"Status: {'ready' if initialized else 'setup required'}"
            ),
            options=_launcher_options(initialized=initialized),
            footer="↑/↓ Move • Enter Select  •  q Exit",
        )
        if option is None or option.key == "exit":
            ui.clear()
            return 0

        if option.disabled:
            ui.clear()
            ui.print_panel(
                "This workflow needs a configured runtime first. Choose Setup to create config.yaml and identity.md.",
                title="Not Ready",
            )
            ui.pause()
            continue

        ui.clear()
        if option.key == "init":
            handle_init(_launcher_args(config_dir=str(config_dir), force=initialized, schema=False))
        elif option.key == "chat":
            handle_chat(
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
        elif option.key == "web":
            handle_web(
                _launcher_args(
                    config_dir=str(config_dir),
                    host=None,
                    port=None,
                    open_browser=True,
                    max_concurrent_chats=None,
                    queue_timeout=None,
                    chat_timeout=None,
                )
            )
        elif option.key == "voice":
            handle_voice(
                _launcher_args(
                    config_dir=str(config_dir),
                    user_id="local_voice",
                    verbose=False,
                    list_devices=False,
                    input_device=None,
                    output_device=None,
                    memory=True,
                )
            )
        elif option.key == "service":
            _run_service_launcher(config_dir)
            continue
        elif option.key == "inspect":
            _run_inspect_launcher(config_dir)
            continue
        elif option.key == "help":
            ui.print_panel(_launcher_help_content(config_dir=config_dir, initialized=initialized), title="xAgent Help")
        else:
            continue

        ui.pause("Press Enter to return to the launcher")


def main(argv: Optional[Sequence[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        if rich_terminal_available():
            return _run_interactive_launcher()
        print_quick_start()
        return 0

    parser = build_parser()
    args = parser.parse_args(argv)

    if not hasattr(args, "handler"):
        print_quick_start()
        return 0

    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
