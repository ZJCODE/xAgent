"""Argument parser assembly for the CLI."""

from __future__ import annotations

import argparse
import sys

from . import agent_runtime, agents, processes_status, runtime, setup, update
from .channels import CHANNEL_API, CHANNEL_FEISHU, CHANNEL_VOICE, CHANNEL_WEIXIN


class XAgentArgumentParser(argparse.ArgumentParser):
    """Root parser with task-oriented help instead of argparse's flat command list."""

    def error(self, message: str) -> None:
        if self.prog == "xagent" and "invalid choice" in message:
            self.print_usage(sys.stderr)
            self.exit(2, "xagent: error: unknown command. Use 'xagent --help' to see available commands.\n")
        if "arguments are required" in message:
            self.print_help(sys.stderr)
            self.exit(2)
        super().error(message)

    def format_help(self) -> str:
        if self.prog != "xagent":
            return super().format_help()
        return "\n".join([
            "usage: xagent <command> ...",
            "",
            "xAgent — your personal AI agent",
            "",
            "Setup:",
            "  setup       Configure the active agent",
            "  agents      Create, select, inspect, or remove agents",
            "  feishu      Configure the Feishu channel",
            "  weixin      Configure the Weixin channel",
            "  update      Update xAgent using its current installation method",
            "",
            "Use Now:",
            "  chat        Chat in the terminal",
            "  web         Manage the browser web UI",
            "  voice       Configure audio or list devices",
            "",
            "Keep Running:",
            "  run         Run this Agent in the foreground",
            "  start       Start this Agent and its enabled channels",
            "  stop        Stop this Agent gracefully",
            "  restart     Apply configuration and restart this Agent",
            "  logs        Read this Agent runtime logs",
            "  status      Show this Agent and channel health",
            "  processes   List or restart all managed background processes",
            "",
            "Inspect:",
            "  config      Show, validate, or locate config.yaml",
            "  memory      List, search, stats, or clear long-term memory",
            "  inspect     Inspect identity, messages, skills, or tasks",
            "  doctor      Check local xAgent readiness",
            "",
            "Advanced:",
            "  observe     Ingest context without generating a reply",
            "  version     Show xAgent version",
            "",
            "Common Flows:",
            "  xagent setup",
            "  xagent update",
            "  xagent agents create work",
            "  xagent agents select work",
            '  xagent chat "Help me plan today"',
            "  xagent start",
            "  xagent web start",
            "  xagent web open",
            "  xagent status",
            "  xagent logs -f",
            "  xagent voice setup",
            "  xagent logs -f",
            "  xagent feishu setup",
            "  xagent start",
            "  xagent weixin setup",
            "  xagent start",
            "  xagent config show",
            "  xagent memory list --days 7",
            "  xagent doctor",
            "",
            "Use 'xagent <command> --help' for detailed options.",
            "",
        ])


def _show_help_on_missing_action(parser: argparse.ArgumentParser) -> None:
    """Make a parser print its help instead of an error when a required
    sub-action / sub-target is omitted (e.g. ``xagent feishu`` without
    ``start`` or ``setup``)."""

    _original_error = parser.error

    def _custom_error(message: str) -> None:
        if "arguments are required" in message:
            parser.print_help(sys.stderr)
            parser.exit(2)
        _original_error(message)

    parser.error = _custom_error  # type: ignore[method-assign]


def _add_agent_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--agent",
        dest="agent",
        default=None,
        help="Managed agent name (default: active agent)",
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
        help=f"Channel(s) to use: api, feishu, weixin, voice, or comma-separated values (default: {default_label})",
    )


def _add_api_runtime_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default=None, help="API host override")
    parser.add_argument("--port", type=int, default=None, help="API port override")
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


def _add_web_client_arguments(parser: argparse.ArgumentParser, *, open_by_default: bool = False) -> None:
    parser.add_argument("--host", default=None, help="Web client host override")
    parser.add_argument("--port", type=int, default=None, help="Web client port override")
    parser.add_argument("--api-url", dest="api_url", default=None, help="Upstream api channel URL")
    if open_by_default:
        parser.add_argument(
            "--open",
            action=argparse.BooleanOptionalAction,
            default=True,
            dest="open_browser",
            help="Open the web client in a browser",
        )
    else:
        parser.add_argument("--open", action="store_true", dest="open_browser", help="Open the web client in a browser")


def _add_voice_runtime_arguments(
    parser: argparse.ArgumentParser,
    *,
    include_list_devices: bool = False,
) -> None:
    parser.add_argument("--user-id", dest="user_id", default="local_voice", help="Speaker identifier")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    if include_list_devices:
        parser.add_argument(
            "--list-devices",
            action="store_true",
            help="List available local audio input/output devices and exit",
        )
    parser.add_argument(
        "--input-device",
        default=None,
        help="Override voice input device by name, #index, index, or auto",
    )
    parser.add_argument(
        "--output-device",
        default=None,
        help="Override voice output device by name, #index, index, or auto",
    )
    parser.add_argument(
        "--profile",
        dest="voice_profile",
        choices=["auto", "room", "headset"],
        default="auto",
        help="Override the profile detected from the audio devices (default: auto)",
    )
    parser.add_argument(
        "--interruptions",
        dest="interruptions",
        choices=["true", "false"],
        default=None,
        help="Override channels.voice.interruptions for this session (true or false)",
    )
    parser.add_argument(
        "--speed",
        dest="speech_speed",
        type=float,
        default=None,
        help="Speaking rate for this session, 0.7-1.3 (default: 1.0)",
    )


def _add_feishu_setup_arguments(parser: argparse.ArgumentParser) -> None:
    _add_agent_argument(parser)
    parser.add_argument("--app-id", dest="app_id", default=None, help="Feishu app id (cli_xxx)")
    parser.add_argument("--app-secret", dest="app_secret", default=None, help="Feishu app secret")
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Enter App ID/Secret manually instead of the one-click QR code flow",
    )
    parser.add_argument(
        "--stream",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Use Feishu streaming cards for in-progress replies",
    )

    parser.add_argument(
        "--group-fetch-limit",
        type=int,
        default=None,
        dest="group_fetch_limit",
        help="How many recent group/topic messages to fetch before replying (default: 10)",
    )
    parser.add_argument(
        "--group-reply-only-when-mentioned",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Record unmentioned group/topic messages but only reply when @mentioned",
    )
    parser.add_argument("--force", action="store_true", help="Overwrite existing channels.feishu config")


def _add_weixin_setup_arguments(parser: argparse.ArgumentParser) -> None:
    _add_agent_argument(parser)
    parser.add_argument("--base-url", default=None, help="Weixin iLink API base URL")
    parser.add_argument("--cdn-base-url", default=None, help="Weixin iLink CDN base URL")
    parser.add_argument("--bot-type", default="3", help="iLink bot_type for QR login (default: 3)")
    parser.add_argument(
        "--owner-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Only allow the QR-authorizing Weixin user to trigger xAgent",
    )
    parser.add_argument(
        "--allow-user",
        action="append",
        default=None,
        dest="allow_users",
        help="Additional Weixin user id allowed to trigger the DM channel; can be repeated or comma-separated",
    )
    parser.add_argument(
        "--media",
        action=argparse.BooleanOptionalAction,
        default=True,
        dest="media_enabled",
        help="Enable inbound/outbound Weixin media download and upload",
    )
    parser.add_argument("--force", action="store_true", help="Overwrite existing channels.weixin config and refresh QR login")


def _add_voice_setup_arguments(parser: argparse.ArgumentParser) -> None:
    _add_agent_argument(parser)
    parser.add_argument("--api-key", dest="api_key", default=None, help="Soniox API key")
    parser.add_argument("--force", action="store_true", help="Overwrite existing channels.voice config")


def _add_channel_lifecycle_subparsers(
    parent_parser: argparse.ArgumentParser,
    channel: str,
    *,
    dest: str,
    has_setup: bool = False,
) -> None:
    """Register start / stop / status / logs / restart (and optionally setup)
    as sub-actions under a top-level channel parser."""

    sub = parent_parser.add_subparsers(dest=dest, metavar="<action>")
    sub.required = True

    if has_setup and channel == CHANNEL_FEISHU:
        setup_parser = sub.add_parser("setup", help="Enable or reconfigure the Feishu channel")
        _add_feishu_setup_arguments(setup_parser)
        setup_parser.set_defaults(handler=setup.handle_init_feishu)
    elif has_setup and channel == CHANNEL_WEIXIN:
        setup_parser = sub.add_parser("setup", help="Enable or reconfigure the Weixin DM channel")
        _add_weixin_setup_arguments(setup_parser)
        setup_parser.set_defaults(handler=setup.handle_init_weixin)

    start_parser = sub.add_parser("start", help=f"Start the {channel} channel in the background")
    _add_agent_argument(start_parser)
    if channel == CHANNEL_API:
        _add_api_runtime_arguments(start_parser)
    start_parser.set_defaults(handler=runtime.handle_start, channels=[channel])

    stop_parser = sub.add_parser("stop", help=f"Stop the background {channel} channel")
    _add_agent_argument(stop_parser)
    stop_parser.set_defaults(handler=runtime.handle_stop, channels=[channel])

    restart_parser = sub.add_parser("restart", help=f"Restart the background {channel} channel")
    _add_agent_argument(restart_parser)
    if channel == CHANNEL_API:
        _add_api_runtime_arguments(restart_parser)
    restart_parser.set_defaults(handler=runtime.handle_restart, channels=[channel])

    status_parser = sub.add_parser("status", help=f"Show {channel} channel status")
    _add_agent_argument(status_parser)
    status_parser.add_argument("--json", action="store_true", dest="json_output", help="Print machine-readable JSON")
    status_parser.set_defaults(handler=runtime.handle_status, channels=[channel])

    logs_parser = sub.add_parser("logs", help=f"Show {channel} channel logs")
    _add_agent_argument(logs_parser)
    logs_parser.add_argument("--lines", type=int, default=80, help="Number of trailing log lines to print")
    logs_parser.add_argument("--follow", "-f", action="store_true", help="Follow log output")
    logs_parser.set_defaults(handler=runtime.handle_logs, channels=[channel])


def _add_web_lifecycle_subparsers(parent_parser: argparse.ArgumentParser) -> None:
    sub = parent_parser.add_subparsers(dest="web_action", metavar="<action>")
    sub.required = True

    open_parser = sub.add_parser("open", help="Open the running web client in a browser")
    _add_agent_argument(open_parser)
    open_parser.set_defaults(handler=runtime.handle_web_open)

    start_parser = sub.add_parser("start", help="Start the web client in the background")
    _add_agent_argument(start_parser)
    _add_web_client_arguments(start_parser)
    start_parser.set_defaults(handler=runtime.handle_web_start)

    stop_parser = sub.add_parser("stop", help="Stop the background web client")
    _add_agent_argument(stop_parser)
    stop_parser.set_defaults(handler=runtime.handle_web_stop)

    restart_parser = sub.add_parser("restart", help="Restart the background web client")
    _add_agent_argument(restart_parser)
    _add_web_client_arguments(restart_parser)
    restart_parser.set_defaults(handler=runtime.handle_web_restart)

    status_parser = sub.add_parser("status", help="Show web client status")
    _add_agent_argument(status_parser)
    status_parser.add_argument("--json", action="store_true", dest="json_output", help="Print machine-readable JSON")
    status_parser.set_defaults(handler=runtime.handle_web_status)

    logs_parser = sub.add_parser("logs", help="Show web client logs")
    _add_agent_argument(logs_parser)
    logs_parser.add_argument("--lines", type=int, default=80, help="Number of trailing log lines to print")
    logs_parser.add_argument("--follow", "-f", action="store_true", help="Follow log output")
    logs_parser.set_defaults(handler=runtime.handle_web_logs)


def _hide_subparser_choice(subparsers: argparse._SubParsersAction, name: str) -> None:
    subparsers._choices_actions = [
        action for action in subparsers._choices_actions if action.dest != name
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = XAgentArgumentParser(
        prog="xagent",
        description="xAgent command line interface",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    # ------------------------------------------------------------------
    # Get Started
    # ------------------------------------------------------------------

    setup_parser = subparsers.add_parser("setup", help="Create or reconfigure config.yaml and identity.md")
    _add_agent_argument(setup_parser)
    setup_parser.add_argument("--force", action="store_true", help="Overwrite setup-managed files")
    setup_parser.set_defaults(handler=setup.handle_init)

    chat_parser = subparsers.add_parser("chat", help="Start an interactive chat or send a single message")
    chat_parser.add_argument("message", nargs="?", help="Single message to send; omit for interactive chat")
    _add_agent_argument(chat_parser)
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
    chat_parser.set_defaults(handler=runtime.handle_chat)

    voice_parser = subparsers.add_parser("voice", help="Configure local microphone/speaker input")
    _add_agent_argument(voice_parser)
    voice_parser.add_argument("--list-devices", action="store_true")
    voice_parser.set_defaults(handler=runtime.handle_voice)
    voice_sub = voice_parser.add_subparsers(dest="voice_action", metavar="<action>")
    voice_setup = voice_sub.add_parser("setup", help="Configure the voice channel")
    _add_voice_setup_arguments(voice_setup)
    voice_setup.set_defaults(handler=setup.handle_init_voice)

    web_parser = subparsers.add_parser("web", help="Manage the browser web client")
    _add_web_lifecycle_subparsers(web_parser)
    _show_help_on_missing_action(web_parser)

    for channel, setup_handler, add_arguments in (
        ("feishu", setup.handle_init_feishu, _add_feishu_setup_arguments),
        ("weixin", setup.handle_init_weixin, _add_weixin_setup_arguments),
    ):
        channel_parser = subparsers.add_parser(channel, help=f"Configure the {channel} channel")
        channel_sub = channel_parser.add_subparsers(dest=f"{channel}_action", metavar="<action>", required=True)
        channel_setup = channel_sub.add_parser("setup", help=f"Configure {channel}")
        add_arguments(channel_setup)
        channel_setup.set_defaults(handler=setup_handler)

    for action in ("run", "start", "stop", "restart", "status", "logs"):
        command_parser = subparsers.add_parser(action, help=f"{action.capitalize()} the Agent runtime")
        _add_agent_argument(command_parser)
        if action in {"run", "start", "restart"}:
            command_parser.add_argument("--channels", action="append", default=None, help="Override enabled channels for this run")
        if action == "status":
            command_parser.add_argument("--json", action="store_true", dest="json_output")
        if action == "logs":
            command_parser.add_argument("--lines", type=int, default=80)
            command_parser.add_argument("--follow", "-f", action="store_true")
        command_parser.set_defaults(handler=getattr(agent_runtime, f"handle_{action}"))

    # ------------------------------------------------------------------
    # Inspect
    # ------------------------------------------------------------------

    config_parser = subparsers.add_parser("config", help="View or validate config.yaml")
    config_sub = config_parser.add_subparsers(dest="config_command", metavar="<action>")
    config_sub.required = True
    for command_name in ("show", "validate", "path"):
        config_cmd = config_sub.add_parser(command_name, help=f"{command_name} config.yaml")
        _add_agent_argument(config_cmd)
        config_cmd.set_defaults(handler=runtime.handle_config)
    _show_help_on_missing_action(config_parser)

    memory_parser = subparsers.add_parser("memory", help="Browse, search, or clear long-term memory")
    memory_sub = memory_parser.add_subparsers(dest="memory_command", metavar="<action>")
    memory_sub.required = True
    for command_name in ("stats", "clear"):
        memory_cmd = memory_sub.add_parser(command_name, help=f"{command_name} memory")
        _add_agent_argument(memory_cmd)
        memory_cmd.add_argument("--scope", default="all", choices=("daily", "weekly", "monthly", "yearly", "all"))
        memory_cmd.add_argument("--yes", action="store_true", help="Confirm destructive operations")
        memory_cmd.set_defaults(handler=runtime.handle_memory)
    memory_list = memory_sub.add_parser("list", help="Show recent daily journals")
    _add_agent_argument(memory_list)
    memory_list.add_argument("--days", type=int, default=setup.DEFAULT_MEMORY_LIST_DAYS, help="Recent natural days to scan")
    memory_list.set_defaults(handler=runtime.handle_memory)
    memory_search = memory_sub.add_parser("search", help="Search memory markdown files")
    _add_agent_argument(memory_search)
    memory_search.add_argument("query", help="Search query")
    memory_search.add_argument("--scope", default="all", choices=("daily", "weekly", "monthly", "yearly", "all"))
    memory_search.set_defaults(handler=runtime.handle_memory)
    _show_help_on_missing_action(memory_parser)

    inspect_parser = subparsers.add_parser("inspect", help="Inspect identity, messages, skills, or tasks")
    inspect_sub = inspect_parser.add_subparsers(dest="inspect_target", metavar="<target>")
    inspect_sub.required = True

    identity_parser = inspect_sub.add_parser("identity", help="Show identity.md information")
    identity_sub = identity_parser.add_subparsers(dest="identity_command", metavar="<action>")
    identity_sub.required = True
    for command_name in ("show", "path"):
        identity_cmd = identity_sub.add_parser(command_name, help=f"{command_name} identity.md")
        _add_agent_argument(identity_cmd)
        identity_cmd.set_defaults(handler=runtime.handle_identity)
    _show_help_on_missing_action(identity_parser)

    messages_parser = inspect_sub.add_parser("messages", help="Inspect or clear the message stream")
    messages_sub = messages_parser.add_subparsers(dest="messages_command", metavar="<action>")
    messages_sub.required = True
    messages_stats = messages_sub.add_parser("stats", help="Show message stream statistics")
    _add_agent_argument(messages_stats)
    messages_stats.set_defaults(handler=runtime.handle_messages)
    messages_list = messages_sub.add_parser("list", help="List recent messages")
    _add_agent_argument(messages_list)
    messages_list.add_argument("--count", type=int, default=20, help="Number of recent messages")
    messages_list.add_argument("--offset", type=int, default=0, help="Number of recent messages to skip")
    messages_list.set_defaults(handler=runtime.handle_messages)
    messages_clear = messages_sub.add_parser("clear", help="Clear all stored messages")
    _add_agent_argument(messages_clear)
    messages_clear.add_argument("--yes", action="store_true", help="Confirm clearing the message stream")
    messages_clear.set_defaults(handler=runtime.handle_messages)
    _show_help_on_missing_action(messages_parser)
    _show_help_on_missing_action(inspect_parser)

    # ------------------------------------------------------------------
    # Other
    # ------------------------------------------------------------------

    doctor_parser = subparsers.add_parser("doctor", help="Check local xAgent readiness")
    _add_agent_argument(doctor_parser)
    _add_channel_argument(doctor_parser, default_label="enabled channels")
    doctor_parser.add_argument("--online", action="store_true", help="Include network/model checks")
    doctor_parser.set_defaults(handler=runtime.handle_doctor)

    processes_parser = subparsers.add_parser(
        "processes",
        help="List or restart all managed background processes",
    )
    processes_sub = processes_parser.add_subparsers(dest="processes_action", metavar="<action>")
    processes_sub.required = True
    processes_status_parser = processes_sub.add_parser("status", help="Show status of all managed processes")
    processes_status_parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Print machine-readable JSON",
    )
    processes_status_parser.set_defaults(handler=processes_status.handle_processes_status)
    processes_restart = processes_sub.add_parser("restart", help="Restart all running managed processes")
    processes_restart.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Print machine-readable JSON",
    )
    processes_restart.set_defaults(handler=processes_status.handle_processes_restart)
    _show_help_on_missing_action(processes_parser)

    observe_parser = subparsers.add_parser("observe", help="Ingest context without generating a reply")
    observe_parser.add_argument("text", help="Observation text to store")
    _add_agent_argument(observe_parser)
    observe_parser.add_argument("--source", default="cli", help="Observation source label")
    observe_parser.add_argument("--event-type", default="observation", help="Observation event type")
    observe_parser.add_argument("--metadata", default=None, help="JSON object with observation metadata")
    observe_parser.set_defaults(handler=runtime.handle_observe)

    version_parser = subparsers.add_parser("version", help="Show xAgent version")
    version_parser.set_defaults(handler=runtime.handle_version)

    update_parser = subparsers.add_parser(
        "update",
        help="Update xAgent using its current installation method",
    )
    update_parser.add_argument(
        "--restart",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Restart running background processes after an update",
    )
    update_parser.set_defaults(handler=update.handle_update)

    agents_parser = subparsers.add_parser("agents", help="Create, select, and inspect managed agents")
    agents_sub = agents_parser.add_subparsers(dest="agents_action", metavar="<action>")
    agents_sub.required = True
    agents_list = agents_sub.add_parser("list", help="List managed agents")
    agents_list.add_argument("--json", action="store_true", dest="json_output", help="Print machine-readable JSON")
    agents_list.set_defaults(handler=agents.handle_agents)
    agents_create = agents_sub.add_parser("create", help="Create a managed agent")
    agents_create.add_argument("name", help="Agent name")
    agents_create.add_argument(
        "--yes",
        action="store_true",
        help="Delete an existing unregistered agent directory without prompting",
    )
    agents_create.set_defaults(handler=agents.handle_agents)
    agents_select = agents_sub.add_parser("select", help="Set the active agent")
    agents_select.add_argument("name", help="Agent name")
    agents_select.set_defaults(handler=agents.handle_agents)
    agents_remove = agents_sub.add_parser("remove", help="Delete a managed agent and its data")
    agents_remove.add_argument("name", help="Agent name")
    agents_remove.add_argument("--yes", action="store_true", help="Confirm deletion without prompting")
    agents_remove.set_defaults(handler=agents.handle_agents)
    agents_info = agents_sub.add_parser("info", help="Show managed agent details")
    agents_info.add_argument("name", help="Agent name")
    agents_info.add_argument("--json", action="store_true", dest="json_output", help="Print machine-readable JSON")
    agents_info.set_defaults(handler=agents.handle_agents)
    _show_help_on_missing_action(agents_parser)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    internal_run = subparsers.add_parser("_run-agent", help=argparse.SUPPRESS)
    _add_agent_argument(internal_run)
    internal_run.add_argument("--config-dir", dest="config_dir", default=None)
    internal_run.add_argument("--channels", action="append", default=None)
    internal_run.set_defaults(handler=agent_runtime.handle_run)
    _hide_subparser_choice(subparsers, "_run-agent")

    internal_web = subparsers.add_parser("_run-web", help=argparse.SUPPRESS)
    _add_agent_argument(internal_web)
    internal_web.add_argument("--config-dir", dest="config_dir", default=None, help=argparse.SUPPRESS)
    _add_web_client_arguments(internal_web)
    internal_web.set_defaults(handler=runtime.handle_run_web_internal)
    _hide_subparser_choice(subparsers, "_run-web")

    internal_update = subparsers.add_parser("_update-worker", help=argparse.SUPPRESS)
    internal_update.add_argument(
        "--restart",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=argparse.SUPPRESS,
    )
    internal_update.set_defaults(handler=update.handle_update_worker)
    _hide_subparser_choice(subparsers, "_update-worker")

    return parser
