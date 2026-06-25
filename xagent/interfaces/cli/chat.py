"""Chat-specific CLI helpers and runtime wrapper."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from ...schemas.attachment import dedupe_attachments
from ...utils.image_utils import workspace_blob_relative_path
from ..base import BaseAgentRunner


def _terminal_ui_class():
    from .. import cli as cli_facade

    return cli_facade.TerminalUI


def _format_cli_workspace_links(content: Any, workspace_dir: str | Path | None) -> str:
    if content is None:
        return ""
    text = str(content)
    if not text or workspace_dir is None:
        return text

    workspace_root = Path(workspace_dir).expanduser().resolve()

    import re

    workspace_blob_cli_link_re = re.compile(
        r'(?:https?://[^\s<>"\')\]]+)?/api/workspace/blob\?path=[^\s<>"\')\]]+',
        re.IGNORECASE,
    )

    def local_workspace_path(match: re.Match[str]) -> str:
        source = match.group(0)
        relative_path = workspace_blob_relative_path(source)
        if not relative_path:
            return source
        candidate = (workspace_root / relative_path).resolve()
        if not candidate.is_relative_to(workspace_root):
            return source
        return candidate.as_posix()

    return workspace_blob_cli_link_re.sub(local_workspace_path, text)


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


def _default_cli_user_id() -> str:
    return "cli_user"


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
    ):
        if stream is None:
            stream = not (logging.getLogger().level <= logging.INFO)

        verbose_mode = logging.getLogger().level <= logging.INFO
        user_id = user_id or _default_cli_user_id()
        await self._chat_interactive_terminal_ui(
            user_id=user_id,
            stream=stream,
            verbose_mode=verbose_mode,
        )

    async def _chat_interactive_terminal_ui(
        self,
        *,
        user_id: str,
        stream: bool,
        verbose_mode: bool,
    ) -> None:
        ui = _terminal_ui_class()()
        self._print_terminal_banner(
            ui,
            stream=stream,
            verbose_mode=verbose_mode,
        )

        while True:
            try:
                user_input = ui.input("[bold cyan]You:[/bold cyan] ").strip()

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
    ):
        user_id = user_id or _default_cli_user_id()
        response = await self.agent(
            user_message=message,
            user_id=user_id,
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
    ) -> None:
        user_id = user_id or _default_cli_user_id()
        await self._print_chat_events(
            user_message=message,
            user_id=user_id,
            stream=stream,
        )

    async def _print_chat_events(
        self,
        *,
        user_message: str,
        user_id: str,
        stream: bool,
    ) -> None:
        line_open = False
        line_has_streamed_text = False
        async for event in self.agent.chat_events(
            user_message=user_message,
            user_id=user_id,
            stream=stream,
            channel="cli",
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
    ) -> None:
        console = ui.console
        line_open = False
        line_has_streamed_text = False

        async for event in self.agent.chat_events(
            user_message=user_message,
            user_id=user_id,
            stream=stream,
            channel="cli",
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
                    f"stream={'on' if stream else 'off'}"
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
                "help               Show this help message",
                "",
                "Built-in tools:",
                *tool_lines,
            ]),
            title="Chat Help",
        )