"""Chat-specific CLI helpers and runtime wrapper."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from pathlib import Path
from typing import Any, Optional

from ...core.runtime import ScheduledDeliveryContext, scheduled_delivery_context
from ...integrations.api.constants import CHANNEL_API
from ...schemas.attachment import dedupe_attachments
from ...utils.image_utils import workspace_blob_relative_path
from ..base import BaseAgentConfig


class LocalRuntimeAgent:
    """Small terminal client; model execution remains in RuntimeHost."""

    def __init__(self, root: Path):
        self.root = root
        self.model = BaseAgentConfig.DEFAULT_MODEL
        self.tools: dict = {}

    async def _client(self):
        from .agent_runtime import ensure_runtime
        client = await ensure_runtime(self.root)
        status = await client.status()
        self.model = str(status.get("model") or self.model)
        self.tools = dict.fromkeys(status.get("tools") or [])
        return client

    async def chat_events(self, **kwargs):
        client = await self._client()
        kwargs.setdefault("channel", "cli")
        async for event in client.chat_events(**kwargs):
            yield event

    async def steer(self, turn_id, content, *, channel=None, user_id=None, room_name=None):
        client = await self._client()
        result = await client.steer(
            turn_id=turn_id,
            content=content,
            channel=channel or "cli",
            user_id=user_id,
            room_name=room_name,
        )
        return bool(result.get("steered"))

    async def __call__(self, **kwargs):
        content = ""
        async for event in self.chat_events(**kwargs):
            if event.get("type") == "error":
                raise RuntimeError(event.get("error") or "Agent turn failed")
            if event.get("type") == "message_done":
                content = str(event.get("content") or "")
        return content


class LocalRuntimeMessages:
    def __init__(self, agent: LocalRuntimeAgent):
        self.agent = agent

    async def clear_messages(self):
        client = await self.agent._client()
        response = await client.request("POST", "/clear_messages")
        response.raise_for_status()


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


def _cli_delivery_context(user_id: str):
    return scheduled_delivery_context(
        ScheduledDeliveryContext(
            channel=CHANNEL_API,
            user_id=user_id,
            target={"user_id": user_id},
            metadata={"source": "cli"},
        )
    )


class AgentCLI:
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

        self.config_dir = Path(config_dir or BaseAgentConfig.DEFAULT_CONFIG_DIR).expanduser().resolve()
        self.config_path = self.config_dir / BaseAgentConfig.CONFIG_FILENAME
        self.workspace_dir = self.config_dir / BaseAgentConfig.WORKSPACE_DIRNAME
        self.agent = LocalRuntimeAgent(self.config_dir)
        self.message_storage = LocalRuntimeMessages(self.agent)
        self._live_turn_id: str | None = None

    async def _pump_cli_events(self, iterator, events: asyncio.Queue) -> None:
        try:
            while True:
                try:
                    event = await iterator.__anext__()
                except StopAsyncIteration:
                    await events.put(None)
                    return
                await events.put(event)
        finally:
            await self._aclose_iterator(iterator)

    @staticmethod
    async def _aclose_iterator(iterator) -> None:
        close = getattr(iterator, "aclose", None)
        if callable(close):
            await close()

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
        if sys.stdin.isatty():
            await self._chat_interactive_tty(
                ui,
                user_id=user_id,
                stream=stream,
                verbose_mode=verbose_mode,
            )
            return

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

    async def _chat_interactive_tty(
        self,
        ui,
        *,
        user_id: str,
        stream: bool,
        verbose_mode: bool,
        lines: asyncio.Queue | None = None,
    ) -> None:
        """Read lines from the terminal while a reply is still printing.

        ``lines`` is a test hook. A real terminal starts one reader thread
        for the whole session so the prompt and the in-turn reader do not
        compete for stdin. Characters typed during streamed output can still
        mix with that output on screen.
        """
        stop_reader: threading.Event | None = None
        if lines is None:
            lines = asyncio.Queue()
            stop_reader = threading.Event()
            self._start_stdin_reader(lines, stop_reader)
        try:
            while True:
                try:
                    user_input = await lines.get()
                    if user_input is None:
                        ui.print_panel("Session interrupted by user.", title="Session Ended")
                        break
                    user_input = str(user_input).strip()
                    if user_input.lower() in ["exit", "quit", "bye"]:
                        ui.print_panel(
                            "Thank you for using xAgent CLI. See you next time.",
                            title="Session Ended",
                        )
                        break
                    if user_input.lower() == "clear":
                        await self.message_storage.clear_messages()
                        ui.print_panel("Global message stream cleared.", title="Cleared")
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
                    await self._print_chat_events_terminal_ui(
                        ui=ui,
                        user_message=user_input,
                        user_id=user_id,
                        stream=stream,
                        lines=lines,
                    )
                except KeyboardInterrupt:
                    ui.print_panel("Session interrupted by user.", title="Session Ended")
                    break
                except Exception as exc:
                    ui.print_panel(f"An error occurred: {exc}", title="Error", border_style="red")
                    if verbose_mode:
                        import traceback

                        traceback.print_exc()
        finally:
            if stop_reader is not None:
                stop_reader.set()

    def _start_stdin_reader(self, lines: asyncio.Queue, stop_reader: threading.Event) -> None:
        loop = asyncio.get_running_loop()

        def read_lines() -> None:
            while not stop_reader.is_set():
                try:
                    line = sys.stdin.readline()
                except Exception:
                    loop.call_soon_threadsafe(lines.put_nowait, None)
                    return
                if line == "":
                    loop.call_soon_threadsafe(lines.put_nowait, None)
                    return
                loop.call_soon_threadsafe(lines.put_nowait, line)

        threading.Thread(target=read_lines, name="xagent-cli-input", daemon=True).start()

    async def chat_single(
        self,
        message: str,
        user_id: Optional[str] = None,
    ):
        user_id = user_id or _default_cli_user_id()
        with _cli_delivery_context(user_id):
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
        with _cli_delivery_context(user_id):
            async for event in self.agent.chat_events(
                user_message=user_message,
                user_id=user_id,
                stream=stream,
                channel="cli",
                inbox_kind="user_turn",
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

    def _render_terminal_chat_event(self, ui: TerminalUI, event: dict, state: dict) -> None:
        console = ui.console
        event_type = event.get("type")
        if event_type == "accepted" and event.get("turn_id"):
            self._live_turn_id = str(event["turn_id"])
        elif event_type == "done" and not event.get("steered"):
            self._live_turn_id = None
        if event_type == "message_start":
            if state["line_open"] and console is not None:
                console.print()
            if console is not None:
                console.print("[magenta]xAgent[/magenta]: ", end="")
            state["line_open"] = True
            state["line_has_streamed_text"] = False
            return
        if event_type == "message_delta":
            if not state["line_open"] and console is not None:
                console.print("[magenta]xAgent[/magenta]: ", end="")
                state["line_open"] = True
                state["line_has_streamed_text"] = False
            delta = self._format_cli_output(event.get("delta", ""))
            if delta and console is not None:
                console.print(delta, end="", markup=False, highlight=False, soft_wrap=True)
                state["line_has_streamed_text"] = True
            return
        if event_type == "message_done":
            attachments_text = self._format_cli_event_attachments(event.get("attachments"))
            content = self._format_cli_output(event.get("content", ""))
            if state["line_has_streamed_text"]:
                if console is not None:
                    console.print()
            elif content:
                ui.print_panel(content, title="xAgent", border_style="green")
            else:
                ui.print_panel("", title="xAgent", border_style="green")
            if attachments_text:
                ui.print_panel(attachments_text, title="Attachments")
            state["line_open"] = False
            state["line_has_streamed_text"] = False
            return
        if event_type == "error":
            ui.print_panel(event.get("error", "Agent processing error."), title="Error", border_style="red")
            state["line_open"] = False
            state["line_has_streamed_text"] = False

    async def _print_chat_events_terminal_ui(
        self,
        *,
        ui: TerminalUI,
        user_message: str,
        user_id: str,
        stream: bool,
        lines: asyncio.Queue | None = None,
    ) -> None:
        console = ui.console
        state = {"line_open": False, "line_has_streamed_text": False}

        with _cli_delivery_context(user_id):
            if lines is None:
                async for event in self.agent.chat_events(
                    user_message=user_message,
                    user_id=user_id,
                    stream=stream,
                    channel="cli",
                    inbox_kind="user_turn",
                ):
                    self._render_terminal_chat_event(ui, event, state)
            else:
                iterator = self.agent.chat_events(
                    user_message=user_message,
                    user_id=user_id,
                    stream=stream,
                    channel="cli",
                    inbox_kind="user_turn",
                ).__aiter__()
                await self._print_events_with_lines(ui, iterator, lines, user_id, state)

        if state["line_open"] and console is not None:
            console.print()

    async def _print_events_with_lines(
        self,
        ui: TerminalUI,
        iterator,
        lines: asyncio.Queue,
        user_id: str,
        state: dict,
    ) -> None:
        """Race streamed events against lines typed during the reply."""
        streamed: asyncio.Queue = asyncio.Queue()
        pump = asyncio.create_task(self._pump_cli_events(iterator, streamed))
        event_task = asyncio.create_task(streamed.get())
        deferred: list[Any] = []
        reading = True
        finished = False
        self._live_turn_id = None
        try:
            while True:
                line_task = asyncio.create_task(lines.get()) if reading else None
                waiting = {event_task}
                if line_task is not None:
                    waiting.add(line_task)
                done, _pending = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                line_ready = line_task is not None and line_task in done
                line_item = line_task.result() if line_ready else None
                if event_task in done:
                    event = event_task.result()
                    if event is None:
                        finished = True
                        if line_ready:
                            deferred.append(line_item)
                        elif line_task is not None:
                            await self._restore_line(line_task, lines)
                        self._return_lines(lines, deferred)
                        return
                    self._render_terminal_chat_event(ui, event, state)
                    if line_ready:
                        if line_item is None:
                            lines.put_nowait(None)
                            reading = False
                        else:
                            await self._steer_or_defer(line_item, user_id, deferred)
                    elif line_task is not None:
                        await self._restore_line(line_task, lines)
                    event_task = asyncio.create_task(streamed.get())
                    continue
                if line_ready:
                    if line_item is None:
                        lines.put_nowait(None)
                        reading = False
                    else:
                        await self._steer_or_defer(line_item, user_id, deferred)
        finally:
            if not event_task.done():
                await self._restore_line(event_task, streamed)
            if not finished and not pump.done():
                pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)

    async def _steer_or_defer(self, line: Any, user_id: str, deferred: list[Any]) -> None:
        text = str(line or "").strip()
        if not text:
            return
        if self._is_between_turn_command(text):
            deferred.append(text)
            return
        turn_id = self._live_turn_id
        if turn_id and await self.agent.steer(turn_id, text, channel="cli", user_id=user_id):
            return
        deferred.append(text)

    @staticmethod
    def _is_between_turn_command(text: str) -> bool:
        lowered = text.lower()
        if lowered in {"exit", "quit", "bye", "clear", "help"}:
            return True
        return lowered.startswith("stream ")

    @staticmethod
    def _return_lines(lines: asyncio.Queue, deferred: list[Any]) -> None:
        pending = []
        while not lines.empty():
            pending.append(lines.get_nowait())
        for item in [*deferred, *pending]:
            lines.put_nowait(item)

    @staticmethod
    async def _restore_line(task: asyncio.Task, lines: asyncio.Queue) -> None:
        if task.done():
            if not task.cancelled() and task.exception() is None:
                lines.put_nowait(task.result())
            return
        task.cancel()
        try:
            item = await task
        except asyncio.CancelledError:
            return
        except Exception:
            return
        lines.put_nowait(item)

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
                "An interactive terminal can type the next message while the agent replies.",
                "A non-interactive terminal waits until the reply finishes, then uses the prompt.",
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
                "While a reply is printing, an interactive terminal reads the next line.",
                "That line joins the running task. If it cannot, it is sent after the reply.",
                "A non-interactive terminal cannot read a line during output.",
                "Type the next message at the prompt after the reply finishes.",
                "Typed characters can mix with streamed text on screen.",
                "Commands such as exit and help apply at the prompt, between replies.",
                "",
                "Built-in tools:",
                *tool_lines,
            ]),
            title="Chat Help",
        )
