import argparse
import asyncio
import getpass
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, Tuple

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
)
from .processes import managed_paths, running_pid, start_background, stop_managed_process, tail_text


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
    return "Attachments:\n" + "\n".join(f"- {path}" for path in paths)

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

        self._print_banner(
            stream=stream,
            memory=memory,
            verbose_mode=verbose_mode,
        )

        while True:
            try:
                user_input = input("\n👤 You: ").strip()

                if user_input.lower() in ["exit", "quit", "bye"]:
                    print("\n╭───────────────────────────────────────╮")
                    print("│  👋 Thank you for using xAgent CLI!   │")
                    print("│         See you next time! 🚀         │")
                    print("╰───────────────────────────────────────╯")
                    break

                if user_input.lower() == "clear":
                    await self.message_storage.clear_messages()
                    print("🧹 ✨ Global message stream cleared.")
                    continue

                if user_input.lower().startswith("stream "):
                    stream_cmd = user_input.lower().split()
                    if len(stream_cmd) == 2 and stream_cmd[1] in {"on", "off"}:
                        stream = stream_cmd[1] == "on"
                        print(f"{'🌊' if stream else '📄'} ✨ Streaming {'enabled' if stream else 'disabled'}.")
                    else:
                        print("⚠️  Usage: stream on/off")
                    continue

                if user_input.lower().startswith("memory "):
                    memory_cmd = user_input.lower().split()
                    if len(memory_cmd) == 2 and memory_cmd[1] in {"on", "off"}:
                        memory = memory_cmd[1] == "on"
                        print(f"{'🧠' if memory else '🚫'} ✨ Memory {'enabled' if memory else 'disabled'}.")
                    else:
                        print("⚠️  Usage: memory on/off")
                    continue

                if user_input.lower() == "help":
                    self._show_help()
                    continue

                if not user_input:
                    print("💭 Please enter a message to chat with the agent.")
                    continue

                if not hasattr(self.agent, "chat_events"):
                    response = await self.agent(
                        user_message=user_input,
                        user_id=user_id,
                        enable_memory=memory,
                    )
                    print("🤖 Agent: " + self._format_cli_output(response))
                    continue

                await self._print_chat_events(
                    user_message=user_input,
                    user_id=user_id,
                    enable_memory=memory,
                    stream=stream,
                )

            except KeyboardInterrupt:
                print("\n\n╭─────────────────────────────────────╮")
                print("│  👋 Session interrupted by user    │")
                print("│      Thank you for using xAgent!   │")
                print("╰─────────────────────────────────────╯")
                break
            except Exception as exc:
                print(f"\n❌ Oops! An error occurred: {exc}")
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

    def _print_banner(
        self,
        stream: bool,
        memory: bool,
        verbose_mode: bool,
    ) -> None:
        config_msg = (
            f"Config: {self.config_path}"
            if self.config_path.is_file()
            else f"Config: default values ({self.config_path} not found)"
        )
        print("xAgent chat")
        print(config_msg)
        print(f"Dir: {self.config_dir}")
        print(f"Model: {self.agent.model}")
        print(f"Tools: {len(self.agent.tools)} loaded")
        print(
            "Status: "
            f"verbose={'on' if verbose_mode else 'off'}, "
            f"stream={'on' if stream else 'off'}, "
            f"memory={'on' if memory else 'off'}"
        )
        print("")
        print("Type a message to chat. Type 'help' for chat commands or 'exit' to quit.")

    def _show_help(self):
        print("\nChat commands:")
        print("  exit, quit, bye    Exit the chat session")
        print("  clear              Clear the agent message stream")
        print("  stream on/off      Toggle streamed delta printing")
        print("  memory on/off      Toggle memory storage mode")
        print("  help               Show this help message")

        print("\nBuilt-in tools:")
        if self.agent.tools:
            for tool_name in self.agent.tools.keys():
                print(f"  {tool_name}")
        else:
            print("  No built-in tools available")


@dataclass(frozen=True)
class InitResult:
    """Result for xagent init file generation."""

    config_path: Path
    identity_path: Path
    memory_dir: Path
    messages_dir: Path
    workspace_dir: Path
    skills_dir: Path
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
MODEL_PLACEHOLDER = "your_model_here"
LANGFUSE_BASE_URL = "https://cloud.langfuse.com"
LANGFUSE_PUBLIC_KEY_PLACEHOLDER = "pk-lf-..."
LANGFUSE_SECRET_KEY_PLACEHOLDER = "sk-lf-..."

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
    "MiniMax-M2.7",
    "MiniMax-M2.7-highspeed",
    "MiniMax-M2.5",
    "MiniMax-M2.5-highspeed",
    "MiniMax-M2.1",
    "MiniMax-M2.1-highspeed",
    "MiniMax-M2",
    "Decide later",
)
QWEN_MODELS = (
    "qwen3.6-plus",
    "qwen3.6-flash",
    "qwen3.6-max-preview",
    "Decide later",
)
OPENAI_SEARCH_PROVIDERS = (
    "openai",
    "qwen",
    "none",
)
NON_OPENAI_SEARCH_PROVIDERS = (
    "none",
    "openai",
    "qwen",
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


def _native_image_generation_provider(provider: str) -> str:
    if provider == PROVIDER_OPENAI:
        return "openai"
    if provider == PROVIDER_MINIMAX:
        return "minimax"
    if provider == PROVIDER_QWEN:
        return "qwen"
    return "none"


def _native_search_provider(provider: str) -> str:
    if provider == PROVIDER_OPENAI:
        return "openai"
    if provider == PROVIDER_QWEN:
        return "qwen"
    return ""


def _image_generation_api_key_placeholder(provider: str) -> str:
    if provider == "openai":
        return OPENAI_IMAGE_API_KEY_PLACEHOLDER
    if provider == "minimax":
        return MINIMAX_IMAGE_API_KEY_PLACEHOLDER
    if provider == "qwen":
        return QWEN_IMAGE_API_KEY_PLACEHOLDER
    return API_KEY_PLACEHOLDER


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
    search_config = {"provider": selection.search_provider or "none"}
    if search_config["provider"] == "openai" and selection.provider != PROVIDER_OPENAI:
        search_config["api_key"] = selection.search_api_key or OPENAI_SEARCH_API_KEY_PLACEHOLDER
    elif search_config["provider"] == "openai" and selection.search_api_key:
        search_config["api_key"] = selection.search_api_key
    elif search_config["provider"] == "qwen" and selection.provider != PROVIDER_QWEN:
        search_config["api_key"] = selection.search_api_key or QWEN_SEARCH_API_KEY_PLACEHOLDER
    elif search_config["provider"] == "qwen" and selection.search_api_key:
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


def _select_search_provider(
    provider: str,
    *,
    input_func: Callable[[str], str] = input,
) -> str:
    options = OPENAI_SEARCH_PROVIDERS if provider == PROVIDER_OPENAI else NON_OPENAI_SEARCH_PROVIDERS
    return _select_option(
        "Search provider",
        options,
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
        selected_model = _select_option(
            "OpenAI model",
            OPENAI_MODELS,
            default_index=1,
            input_func=input_func,
        )
        base_url = OPENAI_BASE_URL
    elif provider == PROVIDER_ANTHROPIC:
        selected_model = _select_option(
            "Anthropic model",
            ANTHROPIC_MODELS,
            default_index=0,
            input_func=input_func,
        )
        base_url = ANTHROPIC_BASE_URL
    elif provider == PROVIDER_DEEPSEEK:
        selected_model = _select_option(
            "DeepSeek model",
            DEEPSEEK_MODELS,
            default_index=0,
            input_func=input_func,
        )
        base_url = DEEPSEEK_BASE_URL
    elif provider == PROVIDER_MINIMAX:
        selected_model = _select_option(
            "MiniMax model",
            MINIMAX_MODELS,
            default_index=0,
            input_func=input_func,
        )
        base_url = MINIMAX_BASE_URL
    elif provider == PROVIDER_QWEN:
        selected_model = _select_option(
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

    native_search_provider = _native_search_provider(provider)
    search_provider = native_search_provider or _select_search_provider(provider, input_func=input_func)
    search_api_key = ""
    if search_provider == "openai" and provider != PROVIDER_OPENAI:
        search_api_key = secret_input_func(
            "OpenAI API key for search (leave blank to fill in later): "
        ).strip()
        if not search_api_key:
            search_api_key = OPENAI_SEARCH_API_KEY_PLACEHOLDER
    elif search_provider == "qwen" and provider != PROVIDER_QWEN:
        search_api_key = secret_input_func(
            "Qwen API key for search (leave blank to fill in later): "
        ).strip()
        if not search_api_key:
            search_api_key = QWEN_SEARCH_API_KEY_PLACEHOLDER

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
    managed_paths = (config_path, identity_path)
    conflicts = tuple(path for path in managed_paths if path.exists())

    if conflicts and not force:
        print("╭─────────────────────────────────────────────────────────╮")
        print("│ xAgent init found existing managed files.              │")
        print("╰─────────────────────────────────────────────────────────╯")
        for path in conflicts:
            print(f"Existing: {path}")
        print("Re-run with --force to overwrite config.yaml and identity.md.")
        return InitResult(
            config_path=config_path,
            identity_path=identity_path,
            memory_dir=memory_dir,
            messages_dir=messages_dir,
            workspace_dir=workspace_dir,
            skills_dir=skills_dir,
            wrote_files=False,
            conflicts=conflicts,
        )

    if clear_runtime_data:
        _clear_runtime_directory(memory_dir)
        _clear_runtime_directory(messages_dir)
        _clear_runtime_directory(workspace_dir)
    memory_dir.mkdir(parents=True, exist_ok=True)
    messages_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    skills_dir.mkdir(parents=True, exist_ok=True)

    selection = selection or _default_init_selection()
    config_path.write_text(_config_yaml(selection, schema=schema), encoding="utf-8")
    identity_path.write_text(selection.identity, encoding="utf-8")

    print("╭─────────────────────────────────────────────────────────╮")
    print("│ xAgent project files written successfully.             │")
    print("╰─────────────────────────────────────────────────────────╯")
    print(f"Config: {config_path}")
    print(f"Identity: {identity_path}")
    print(f"Memory: {memory_dir}")
    print(f"Messages: {messages_dir}")
    print(f"Workspace: {workspace_dir}")
    print(f"Skills: {skills_dir}")
    return InitResult(
        config_path=config_path,
        identity_path=identity_path,
        memory_dir=memory_dir,
        messages_dir=messages_dir,
        workspace_dir=workspace_dir,
        skills_dir=skills_dir,
        wrote_files=True,
        conflicts=(),
    )


def _clear_runtime_directory(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


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
    if args.force:
        clear_runtime_data = _prompt_yes_no(
            "Clear existing memory/, messages/, and workspace/ data as part of init --force?",
            default=False,
        )

    selection = collect_init_selection()
    result = init_agent_directory(
        args.config_dir,
        force=args.force,
        schema=args.schema,
        selection=selection,
        clear_runtime_data=clear_runtime_data,
    )
    return 0 if result.wrote_files else 1


async def _flush_chat_exit_memory(agent: Any) -> None:
    flusher = getattr(agent, "flush_memory", None)
    if flusher is None:
        return
    print("⏳ 正在写入退出前记忆，请稍候...")
    await flusher()


def handle_chat(args: argparse.Namespace) -> int:
    agent_cli = AgentCLI(config_dir=args.config_dir, verbose=args.verbose)

    if args.message is None:
        async def run_interactive_chat():
            try:
                await agent_cli.chat_interactive(
                    user_id=args.user_id,
                    stream=args.stream,
                    memory=args.memory,
                )
            finally:
                await _flush_chat_exit_memory(agent_cli.agent)

        asyncio.run(run_interactive_chat())
        return 0

    event_mode = bool(args.events or args.stream is not None or hasattr(agent_cli.agent, "chat_events"))
    stream = bool(args.stream) if args.stream is not None else False

    async def run_single_message():
        try:
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
        finally:
            await _flush_chat_exit_memory(agent_cli.agent)

    asyncio.run(run_single_message())
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


def handle_init_feishu(args: argparse.Namespace) -> int:
    config_path = _config_path(args)
    if not config_path.is_file():
        print(f"Config not found: {config_path}")
        print("Run: xagent init")
        return 1

    try:
        with config_path.open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        print(f"Invalid YAML in {config_path}: {exc}")
        return 1
    if not isinstance(config, dict):
        print(f"Configuration must be a mapping: {config_path}")
        return 1

    channels_cfg = config.setdefault("channels", {})
    if not isinstance(channels_cfg, dict):
        print("channels must be a dictionary")
        return 1
    if "feishu" in channels_cfg and not args.force:
        print("channels.feishu already exists. Use --force to overwrite.")
        return 1

    print("")
    print("Feishu setup guide:\n")
    print("1. Create an agent: https://open.feishu.cn/page/launcher")
    print("2. Copy your App ID and App Secret.")
    print("")

    app_id = args.app_id or input("Feishu App ID: ").strip()
    if not app_id:
        print("App ID is required.")
        return 1
    app_secret = args.app_secret or getpass.getpass("Feishu App Secret: ").strip()
    if not app_secret:
        print("App Secret is required.")
        return 1

    api_cfg = channels_cfg.setdefault("api", {})
    if isinstance(api_cfg, dict):
        api_cfg.setdefault("host", BaseAgentConfig.DEFAULT_HOST)
        api_cfg.setdefault("port", BaseAgentConfig.DEFAULT_PORT)

    channels_cfg["feishu"] = {
        "app_id": app_id,
        "app_secret": app_secret,
        "stream": False,
        "enable_memory": True,
        "group_history_count": 10,
    }

    config_path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=False), encoding="utf-8")
    print(f"\nUpdated {config_path} with channels.feishu\n")
    print("===== Finish setup in the Feishu Developer Console =====\n")
    print("1. Open your agent: https://open.feishu.cn/app")
    print("2. Add extra permissions:")
    print("  - im:message.group_msg (for group chats)")
    print("  - im:message.group_at_msg.include_bot:readonly (for group @mentions from users and bots)")
    print("  - contact:user.base:readonly (for user display names)")
    print("  - admin:app.info:readonly (for other bot or agent display names)")
    print("\nRun: `xagent service start feishu` or `xagent service start all` to start your bot.\n")
    print("======================================================\n")
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
        try:
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
        finally:
            await runner.agent.flush_memory()

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
        print("  xagent doctor")
    else:
        print("Quick start:")
        print("  xagent chat")
        print("  xagent web")
        print("  xagent service")
        print("  xagent doctor")
    print("")
    print("Use 'xagent --help' to see all commands.")


def main(argv: Optional[Sequence[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
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
