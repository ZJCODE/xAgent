"""Thin interactive shell over the public single-Runtime CLI."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from ...core.runtime import RuntimeLauncher
from .agents import (
    AgentRegistryError,
    load_agent_registry,
    management_root,
    select_agent,
)
from .terminal_ui import MenuOption, ReturnToLauncherHome, TerminalUI


CommandDispatcher = Callable[[Sequence[str]], int]
CHANNEL_NAMES = ("api", "feishu", "weixin", "voice")


def handle_launcher(args) -> int:
    from . import main

    return run_launcher(
        dispatch=main,
        initial_agent=getattr(args, "agent", None),
    )


def run_launcher(
    *,
    dispatch: CommandDispatcher,
    ui: TerminalUI | None = None,
    initial_agent: str | None = None,
    registry_root: Path | None = None,
) -> int:
    """Run a navigation layer that delegates every operation to public commands."""
    terminal = ui or TerminalUI()
    root = (registry_root or management_root()).expanduser().resolve()
    selected_agent = initial_agent
    requested_agent = initial_agent

    while True:
        try:
            registry = load_agent_registry(root=root)
        except AgentRegistryError:
            choice = terminal.select_menu(
                title="xAgent",
                subtitle="No Agent is configured.",
                options=[
                    MenuOption("setup", "Set up Agent", "Create the first Agent."),
                    MenuOption("exit", "Exit", "Close the launcher."),
                ],
                footer="↑/↓ Move · Enter Select · q Exit",
            )
            if choice is None or choice.key == "exit":
                return 0
            _dispatch(terminal, dispatch, ["setup"])
            continue

        if requested_agent is not None:
            if requested_agent not in registry.agents:
                terminal.print_panel(
                    f"Unknown Agent: {requested_agent}",
                    title="Launcher Stopped",
                    border_style="red",
                )
                return 1
            selected_agent = requested_agent
            requested_agent = None
        elif selected_agent not in registry.agents:
            selected_agent = registry.active_agent
        entry = registry.agents[selected_agent]
        status = RuntimeLauncher(entry.path).status()
        runtime_label = _runtime_label(status)
        choice = terminal.select_menu(
            title="xAgent",
            subtitle=f"Agent: {entry.title} ({entry.name}) · {runtime_label}",
            options=[
                MenuOption("chat", "Chat", "Talk through the single Runtime."),
                MenuOption("runtime", "Runtime", "Start, stop, restart, inspect, or view logs."),
                MenuOption("channels", "Channels", "Hot-start or stop one transport."),
                MenuOption(
                    "operations",
                    "Operations",
                    "Inspect blocked deliveries and people.",
                    disabled=status is None,
                ),
                MenuOption("agents", "Agents", "Switch the launcher target."),
                MenuOption("exit", "Exit", "Close the launcher; Runtime keeps running."),
            ],
            footer="↑/↓ Move · Enter Select · q Exit",
        )
        if choice is None or choice.key == "exit":
            return 0
        target = ["--agent", entry.name]
        try:
            if choice.key == "chat":
                _dispatch(terminal, dispatch, ["chat", *target], pause=False)
            elif choice.key == "runtime":
                _runtime_menu(terminal, dispatch, entry.name, entry.path)
            elif choice.key == "channels":
                _channel_menu(terminal, dispatch, entry.name, entry.path, status)
            elif choice.key == "operations":
                _operations_menu(terminal, dispatch, entry.name)
            elif choice.key == "agents":
                selected_agent = _agent_menu(terminal, root, selected_agent)
        except ReturnToLauncherHome:
            continue


def _runtime_menu(
    ui: TerminalUI,
    dispatch: CommandDispatcher,
    agent_name: str,
    config_dir: Path,
) -> None:
    while True:
        status = RuntimeLauncher(config_dir).status()
        running = status is not None
        options = [
            MenuOption("status", "Status", "Show Runtime and channel state."),
            MenuOption("start", "Start", "Start the Runtime.", disabled=running),
            MenuOption("stop", "Stop", "Gracefully stop the Runtime.", disabled=not running),
            MenuOption("restart", "Restart", "Gracefully replace the Runtime.", disabled=not running),
            MenuOption("logs", "Logs", "Show the latest Runtime log lines."),
            MenuOption("back", "Back", "Return to the launcher."),
        ]
        choice = ui.select_menu(
            title="Runtime",
            subtitle=_runtime_label(status),
            options=options,
        )
        if choice is None or choice.key == "back":
            return
        if choice.key == "logs":
            ui.print_panel(
                _tail(config_dir / "run" / "runtime.log"),
                title="Runtime Logs",
            )
            ui.pause()
            continue
        _dispatch(ui, dispatch, [choice.key, "--agent", agent_name])


def _channel_menu(
    ui: TerminalUI,
    dispatch: CommandDispatcher,
    agent_name: str,
    config_dir: Path,
    status: dict | None,
) -> None:
    rows = {
        str(row.get("name")): row
        for row in (status or {}).get("channels", [])
    }
    while True:
        options = []
        for name in CHANNEL_NAMES:
            row = rows.get(name, {})
            state = str(row.get("state") or "runtime-stopped")
            enabled = bool(row.get("enabled"))
            options.append(
                MenuOption(
                    name,
                    name.capitalize(),
                    f"{state} · enabled={str(enabled).lower()}",
                )
            )
        options.append(MenuOption("back", "Back", "Return to the launcher."))
        selected = ui.select_menu(title="Channels", options=options)
        if selected is None or selected.key == "back":
            return
        _channel_actions(ui, dispatch, agent_name, selected.key)
        current = RuntimeLauncher(config_dir).status()
        rows = {
            str(row.get("name")): row
            for row in (current or {}).get("channels", [])
        }


def _channel_actions(
    ui: TerminalUI,
    dispatch: CommandDispatcher,
    agent_name: str,
    channel: str,
) -> None:
    choice = ui.select_menu(
        title=channel.capitalize(),
        options=[
            MenuOption("start", "Start", "Enable and start this channel."),
            MenuOption("stop", "Stop", "Disable and gracefully stop this channel."),
            MenuOption("restart", "Restart", "Restart only this channel."),
            MenuOption("setup", "Setup", "Configure this channel."),
            MenuOption("back", "Back", "Return to channels."),
        ],
    )
    if choice is None or choice.key == "back":
        return
    command = ["channel", choice.key, channel, "--agent", agent_name]
    _dispatch(ui, dispatch, command)


def _operations_menu(
    ui: TerminalUI,
    dispatch: CommandDispatcher,
    agent_name: str,
) -> None:
    while True:
        choice = ui.select_menu(
            title="Operations",
            options=[
                MenuOption(
                    "blocked",
                    "Blocked deliveries",
                    "List outbound work awaiting explicit retry.",
                ),
                MenuOption(
                    "retry",
                    "Retry delivery",
                    "Retry one blocked delivery by ID.",
                ),
                MenuOption("people", "People", "List explicit channel-account links."),
                MenuOption(
                    "link",
                    "Link account",
                    "Explicitly link one channel account to a person.",
                ),
                MenuOption("back", "Back", "Return to the launcher."),
            ],
        )
        if choice is None or choice.key == "back":
            return
        if choice.key == "blocked":
            command = [
                "delivery",
                "list",
                "--status",
                "blocked",
                "--agent",
                agent_name,
            ]
        elif choice.key == "retry":
            delivery_id = ui.ask_text("Delivery ID").strip()
            if not delivery_id:
                continue
            command = [
                "delivery",
                "retry",
                delivery_id,
                "--agent",
                agent_name,
            ]
        elif choice.key == "people":
            command = ["person", "list", "--agent", agent_name]
        else:
            person_id = ui.ask_text("Person ID").strip()
            channel = ui.select(
                label="Channel",
                options=[
                    MenuOption(name, name, "")
                    for name in CHANNEL_NAMES
                ],
            )
            account_id = ui.ask_text("Channel account ID").strip()
            if not person_id or channel is None or not account_id:
                continue
            command = [
                "person",
                "link",
                person_id,
                channel.key,
                account_id,
                "--agent",
                agent_name,
            ]
        _dispatch(ui, dispatch, command)


def _agent_menu(
    ui: TerminalUI,
    root: Path,
    selected_agent: str,
) -> str:
    registry = load_agent_registry(root=root)
    names = tuple(sorted(registry.agents))
    options = [
        MenuOption(
            name,
            registry.agents[name].title,
            str(registry.agents[name].path),
        )
        for name in names
    ]
    options.append(MenuOption("back", "Back", "Return to the launcher."))
    default_index = names.index(selected_agent) if selected_agent in names else 0
    choice = ui.select_menu(
        title="Agents",
        options=options,
        default_index=default_index,
    )
    if choice is None or choice.key == "back":
        return selected_agent
    select_agent(choice.key, root=root)
    return choice.key


def _dispatch(
    ui: TerminalUI,
    dispatch: CommandDispatcher,
    command: Sequence[str],
    *,
    pause: bool = True,
) -> int:
    ui.clear()
    result = int(dispatch(command) or 0)
    if pause:
        ui.pause()
    return result


def _runtime_label(status: dict | None) -> str:
    if status is None:
        return "Runtime stopped"
    channels = status.get("channels", [])
    running = sum(1 for row in channels if row.get("state") == "running")
    return f"Runtime pid {status['pid']} · {running}/{len(channels)} channels running"


def _tail(path: Path, *, lines: int = 80) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "No Runtime log is available."
    selected = content.splitlines()[-lines:]
    return "\n".join(selected) or "Runtime log is empty."
