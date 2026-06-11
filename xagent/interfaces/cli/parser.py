"""Argument parser assembly for the CLI."""

from __future__ import annotations

import argparse
import sys

from .channels import CHANNEL_API, CHANNEL_FEISHU, CHANNEL_WEIXIN
from . import runtime, setup


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
            "Get Started:",
            "  setup       Create or reconfigure config.yaml and identity.md",
            "  chat        Start an interactive chat or send a single message",
            "  voice       Talk with your agent by microphone",
            "  web         Open the web UI",
            "",
            "Channels:",
            "  api         Manage the API / Web UI background service",
            "  feishu      Manage the Feishu bot",
            "  weixin      Manage the Weixin DM channel",
            "  status      Show running status of all channels",
            "",
            "Inspect:",
            "  config      View or validate config.yaml",
            "  memory      Browse, search, or clear long-term memory",
            "  inspect     Inspect identity, messages, skills, or tasks",
            "  doctor      Check local xAgent readiness",
            "",
            "Advanced:",
            "  observe     Ingest context without generating a reply",
            "  version     Show xAgent version",
            "",
            "Examples:",
            "  xagent setup",
            '  xagent chat "Help me plan today"',
            "  xagent web",
            "  xagent api start",
            "  xagent api logs -f",
            "  xagent feishu setup",
            "  xagent feishu start",
            "  xagent status",
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
        help=f"Channel(s) to use: api, feishu, weixin, or comma-separated values (default: {default_label})",
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


def _add_feishu_setup_arguments(parser: argparse.ArgumentParser) -> None:
    _add_dir_argument(parser)
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
        "--group-reply-without-mention",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Route every group/topic message, even without an @mention",
    )
    parser.add_argument("--force", action="store_true", help="Overwrite existing channels.feishu config")


def _add_weixin_setup_arguments(parser: argparse.ArgumentParser) -> None:
    _add_dir_argument(parser)
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


def _add_channel_lifecycle_subparsers(
    parent_parser: argparse.ArgumentParser,
    channel: str,
    *,
    dest: str,
    has_setup: bool = False,
    has_open: bool = False,
) -> None:
    """Register start / stop / status / logs / restart (and optionally setup / open)
    as sub-actions under a top-level channel parser."""

    sub = parent_parser.add_subparsers(dest=dest, metavar="<action>")
    sub.required = True

    if has_open:
        open_parser = sub.add_parser("open", help="Start API in foreground and open the browser")
        _add_dir_argument(open_parser)
        _add_api_runtime_arguments(open_parser, open_by_default=True)
        open_parser.set_defaults(handler=runtime.handle_web)

    if has_setup and channel == CHANNEL_FEISHU:
        setup_parser = sub.add_parser("setup", help="Enable or reconfigure the Feishu channel")
        _add_feishu_setup_arguments(setup_parser)
        setup_parser.set_defaults(handler=setup.handle_init_feishu)
    elif has_setup and channel == CHANNEL_WEIXIN:
        setup_parser = sub.add_parser("setup", help="Enable or reconfigure the Weixin DM channel")
        _add_weixin_setup_arguments(setup_parser)
        setup_parser.set_defaults(handler=setup.handle_init_weixin)

    start_parser = sub.add_parser("start", help=f"Start the {channel} channel in the background")
    _add_dir_argument(start_parser)
    if channel == CHANNEL_API:
        _add_api_runtime_arguments(start_parser)
    start_parser.set_defaults(handler=runtime.handle_start, channels=[channel])

    stop_parser = sub.add_parser("stop", help=f"Stop the background {channel} channel")
    _add_dir_argument(stop_parser)
    stop_parser.set_defaults(handler=runtime.handle_stop, channels=[channel])

    restart_parser = sub.add_parser("restart", help=f"Restart the background {channel} channel")
    _add_dir_argument(restart_parser)
    if channel == CHANNEL_API:
        _add_api_runtime_arguments(restart_parser)
    restart_parser.set_defaults(handler=runtime.handle_restart, channels=[channel])

    status_parser = sub.add_parser("status", help=f"Show {channel} channel status")
    _add_dir_argument(status_parser)
    status_parser.add_argument("--json", action="store_true", dest="json_output", help="Print machine-readable JSON")
    status_parser.set_defaults(handler=runtime.handle_status, channels=[channel])

    logs_parser = sub.add_parser("logs", help=f"Show {channel} channel logs")
    _add_dir_argument(logs_parser)
    logs_parser.add_argument("--lines", type=int, default=80, help="Number of trailing log lines to print")
    logs_parser.add_argument("--follow", "-f", action="store_true", help="Follow log output")
    logs_parser.set_defaults(handler=runtime.handle_logs, channels=[channel])


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
    _add_dir_argument(setup_parser)
    setup_parser.add_argument("--force", action="store_true", help="Overwrite setup-managed files")
    setup_parser.add_argument("--schema", action="store_true", help="Include a starter output_schema example")
    setup_parser.set_defaults(handler=setup.handle_init)

    chat_parser = subparsers.add_parser("chat", help="Start an interactive chat or send a single message")
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
    chat_parser.set_defaults(handler=runtime.handle_chat)

    voice_parser = subparsers.add_parser("voice", help="Talk with your agent by microphone")
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
    voice_parser.set_defaults(handler=runtime.handle_voice)

    web_parser = subparsers.add_parser("web", help="Open the web UI")
    _add_dir_argument(web_parser)
    _add_api_runtime_arguments(web_parser, open_by_default=True)
    web_parser.set_defaults(handler=runtime.handle_web)

    # ------------------------------------------------------------------
    # Channels
    # ------------------------------------------------------------------

    api_parser = subparsers.add_parser("api", help="Manage the API / Web UI background service")
    _add_channel_lifecycle_subparsers(api_parser, CHANNEL_API, dest="api_action", has_open=True)
    _show_help_on_missing_action(api_parser)

    feishu_parser = subparsers.add_parser("feishu", help="Manage the Feishu bot")
    _add_channel_lifecycle_subparsers(feishu_parser, CHANNEL_FEISHU, dest="feishu_action", has_setup=True)
    _show_help_on_missing_action(feishu_parser)

    weixin_parser = subparsers.add_parser("weixin", help="Manage the Weixin DM channel")
    _add_channel_lifecycle_subparsers(weixin_parser, CHANNEL_WEIXIN, dest="weixin_action", has_setup=True)
    _show_help_on_missing_action(weixin_parser)

    status_parser = subparsers.add_parser("status", help="Show running status of all channels")
    _add_dir_argument(status_parser)
    status_parser.add_argument("--json", action="store_true", dest="json_output", help="Print machine-readable JSON")
    status_parser.set_defaults(handler=runtime.handle_status_all)

    # ------------------------------------------------------------------
    # Inspect
    # ------------------------------------------------------------------

    config_parser = subparsers.add_parser("config", help="View or validate config.yaml")
    config_sub = config_parser.add_subparsers(dest="config_command", metavar="<action>")
    config_sub.required = True
    for command_name in ("show", "validate", "path"):
        config_cmd = config_sub.add_parser(command_name, help=f"{command_name} config.yaml")
        _add_dir_argument(config_cmd)
        config_cmd.set_defaults(handler=runtime.handle_config)
    _show_help_on_missing_action(config_parser)

    memory_parser = subparsers.add_parser("memory", help="Browse, search, or clear long-term memory")
    memory_sub = memory_parser.add_subparsers(dest="memory_command", metavar="<action>")
    memory_sub.required = True
    for command_name in ("stats", "clear"):
        memory_cmd = memory_sub.add_parser(command_name, help=f"{command_name} memory")
        _add_dir_argument(memory_cmd)
        memory_cmd.add_argument("--scope", default="all", choices=("daily", "weekly", "monthly", "yearly", "all"))
        memory_cmd.add_argument("--yes", action="store_true", help="Confirm destructive operations")
        memory_cmd.set_defaults(handler=runtime.handle_memory)
    memory_list = memory_sub.add_parser("list", help="Show recent daily journals")
    _add_dir_argument(memory_list)
    memory_list.add_argument("--days", type=int, default=setup.DEFAULT_MEMORY_LIST_DAYS, help="Recent natural days to scan")
    memory_list.set_defaults(handler=runtime.handle_memory)
    memory_search = memory_sub.add_parser("search", help="Search memory markdown files")
    _add_dir_argument(memory_search)
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
        _add_dir_argument(identity_cmd)
        identity_cmd.set_defaults(handler=runtime.handle_identity)
    _show_help_on_missing_action(identity_parser)

    messages_parser = inspect_sub.add_parser("messages", help="Inspect or clear the message stream")
    messages_sub = messages_parser.add_subparsers(dest="messages_command", metavar="<action>")
    messages_sub.required = True
    messages_stats = messages_sub.add_parser("stats", help="Show message stream statistics")
    _add_dir_argument(messages_stats)
    messages_stats.set_defaults(handler=runtime.handle_messages)
    messages_list = messages_sub.add_parser("list", help="List recent messages")
    _add_dir_argument(messages_list)
    messages_list.add_argument("--count", type=int, default=20, help="Number of recent messages")
    messages_list.add_argument("--offset", type=int, default=0, help="Number of recent messages to skip")
    messages_list.set_defaults(handler=runtime.handle_messages)
    messages_clear = messages_sub.add_parser("clear", help="Clear all stored messages")
    _add_dir_argument(messages_clear)
    messages_clear.add_argument("--yes", action="store_true", help="Confirm clearing the message stream")
    messages_clear.set_defaults(handler=runtime.handle_messages)
    _show_help_on_missing_action(messages_parser)
    _show_help_on_missing_action(inspect_parser)

    # ------------------------------------------------------------------
    # Other
    # ------------------------------------------------------------------

    doctor_parser = subparsers.add_parser("doctor", help="Check local xAgent readiness")
    _add_dir_argument(doctor_parser)
    _add_channel_argument(doctor_parser, default_label="enabled channels")
    doctor_parser.add_argument("--online", action="store_true", help="Include network/model checks")
    doctor_parser.set_defaults(handler=runtime.handle_doctor)

    observe_parser = subparsers.add_parser("observe", help="Ingest context without generating a reply")
    observe_parser.add_argument("text", help="Observation text to store")
    _add_dir_argument(observe_parser)
    observe_parser.add_argument("--source", default="cli", help="Observation source label")
    observe_parser.add_argument("--event-type", default="observation", help="Observation event type")
    observe_parser.add_argument("--metadata", default=None, help="JSON object with observation metadata")
    observe_parser.set_defaults(handler=runtime.handle_observe)

    version_parser = subparsers.add_parser("version", help="Show xAgent version")
    version_parser.set_defaults(handler=runtime.handle_version)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    internal_run = subparsers.add_parser("_run-channel", help=argparse.SUPPRESS)
    internal_run.add_argument("channel", choices=(CHANNEL_API, CHANNEL_FEISHU, CHANNEL_WEIXIN))
    _add_dir_argument(internal_run)
    _add_api_runtime_arguments(internal_run)
    internal_run.set_defaults(handler=runtime.handle_run_channel_internal)
    _hide_subparser_choice(subparsers, "_run-channel")

    return parser
