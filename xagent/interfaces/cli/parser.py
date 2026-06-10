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
            "  channel   Open, start, stop, inspect, and tail channels",
            "  doctor    Check local xAgent readiness",
            "",
            "Advanced:",
            "  inspect   Inspect configuration, identity, memory, or messages",
            "  version   Show xAgent version",
            "",
            "Examples:",
            "  xagent init",
            '  xagent chat "Help me plan today"',
            "  xagent voice",
            "  xagent web",
            "  xagent channel api start",
            "  xagent channel feishu logs -f",
            "",
            "Use 'xagent <command> --help' for command-specific help.",
            "",
        ])


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
        "--group-history-count",
        type=int,
        default=None,
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

    init_parser = subparsers.add_parser("init", help="Create config.yaml and identity.md")
    _add_dir_argument(init_parser)
    init_parser.add_argument("--force", action="store_true", help="Overwrite init-managed files")
    init_parser.add_argument("--schema", action="store_true", help="Include a starter output_schema example")
    init_parser.set_defaults(handler=setup.handle_init)

    init_sub = init_parser.add_subparsers(dest="init_target", metavar="[target]")
    init_feishu = init_sub.add_parser("feishu", help="Enable and configure the Feishu channel")
    _add_feishu_setup_arguments(init_feishu)
    init_feishu.set_defaults(handler=setup.handle_init_feishu)
    init_weixin = init_sub.add_parser("weixin", help="Enable and configure the Weixin channel")
    _add_weixin_setup_arguments(init_weixin)
    init_weixin.set_defaults(handler=setup.handle_init_weixin)

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
    chat_parser.set_defaults(handler=runtime.handle_chat)

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
    voice_parser.set_defaults(handler=runtime.handle_voice)

    web_parser = subparsers.add_parser("web", help="Open the built-in web UI")
    _add_dir_argument(web_parser)
    _add_api_runtime_arguments(web_parser, open_by_default=True)
    web_parser.set_defaults(handler=runtime.handle_web)

    observe_parser = subparsers.add_parser("observe", help="Ingest context without generating a reply")
    observe_parser.add_argument("text", help="Observation text to store")
    _add_dir_argument(observe_parser)
    observe_parser.add_argument("--source", default="cli", help="Observation source label")
    observe_parser.add_argument("--event-type", default="observation", help="Observation event type")
    observe_parser.add_argument("--metadata", default=None, help="JSON object with observation metadata")
    observe_parser.set_defaults(handler=runtime.handle_observe)

    channel_parser = subparsers.add_parser("channel", help="Open and manage runtime channels")
    channel_sub = channel_parser.add_subparsers(dest="channel_target", metavar="<channel>")
    channel_sub.required = True

    api_channel = channel_sub.add_parser("api", help="Open or manage the API / Web UI channel")
    api_sub = api_channel.add_subparsers(dest="channel_action", metavar="<action>")
    api_sub.required = True
    api_open = api_sub.add_parser("open", help="Open the API channel and browser UI in the foreground")
    _add_dir_argument(api_open)
    _add_api_runtime_arguments(api_open, open_by_default=True)
    api_open.set_defaults(handler=runtime.handle_web)
    api_start = api_sub.add_parser("start", help="Start the API channel in the background")
    _add_dir_argument(api_start)
    _add_api_runtime_arguments(api_start)
    api_start.set_defaults(handler=runtime.handle_start, channels=[CHANNEL_API])
    api_stop = api_sub.add_parser("stop", help="Stop the background API channel")
    _add_dir_argument(api_stop)
    api_stop.set_defaults(handler=runtime.handle_stop, channels=[CHANNEL_API])
    api_restart = api_sub.add_parser("restart", help="Restart the background API channel")
    _add_dir_argument(api_restart)
    _add_api_runtime_arguments(api_restart)
    api_restart.set_defaults(handler=runtime.handle_restart, channels=[CHANNEL_API])
    api_status = api_sub.add_parser("status", help="Show API channel status")
    _add_dir_argument(api_status)
    api_status.add_argument("--json", action="store_true", dest="json_output", help="Print machine-readable JSON")
    api_status.set_defaults(handler=runtime.handle_status, channels=[CHANNEL_API])
    api_logs = api_sub.add_parser("logs", help="Show API channel logs")
    _add_dir_argument(api_logs)
    api_logs.add_argument("--lines", type=int, default=80, help="Number of trailing log lines to print")
    api_logs.add_argument("--follow", "-f", action="store_true", help="Follow log output")
    api_logs.set_defaults(handler=runtime.handle_logs, channels=[CHANNEL_API])

    feishu_channel = channel_sub.add_parser("feishu", help="Configure or manage the Feishu channel")
    feishu_sub = feishu_channel.add_subparsers(dest="channel_action", metavar="<action>")
    feishu_sub.required = True
    feishu_setup = feishu_sub.add_parser("setup", help="Enable or reconfigure the Feishu channel")
    _add_feishu_setup_arguments(feishu_setup)
    feishu_setup.set_defaults(handler=setup.handle_init_feishu)
    feishu_start = feishu_sub.add_parser("start", help="Start the Feishu channel in the background")
    _add_dir_argument(feishu_start)
    feishu_start.set_defaults(handler=runtime.handle_start, channels=[CHANNEL_FEISHU])
    feishu_stop = feishu_sub.add_parser("stop", help="Stop the background Feishu channel")
    _add_dir_argument(feishu_stop)
    feishu_stop.set_defaults(handler=runtime.handle_stop, channels=[CHANNEL_FEISHU])
    feishu_restart = feishu_sub.add_parser("restart", help="Restart the background Feishu channel")
    _add_dir_argument(feishu_restart)
    feishu_restart.set_defaults(handler=runtime.handle_restart, channels=[CHANNEL_FEISHU])
    feishu_status = feishu_sub.add_parser("status", help="Show Feishu channel status")
    _add_dir_argument(feishu_status)
    feishu_status.add_argument("--json", action="store_true", dest="json_output", help="Print machine-readable JSON")
    feishu_status.set_defaults(handler=runtime.handle_status, channels=[CHANNEL_FEISHU])
    feishu_logs = feishu_sub.add_parser("logs", help="Show Feishu channel logs")
    _add_dir_argument(feishu_logs)
    feishu_logs.add_argument("--lines", type=int, default=80, help="Number of trailing log lines to print")
    feishu_logs.add_argument("--follow", "-f", action="store_true", help="Follow log output")
    feishu_logs.set_defaults(handler=runtime.handle_logs, channels=[CHANNEL_FEISHU])

    weixin_channel = channel_sub.add_parser("weixin", help="Configure or manage the Weixin DM channel")
    weixin_sub = weixin_channel.add_subparsers(dest="channel_action", metavar="<action>")
    weixin_sub.required = True
    weixin_setup = weixin_sub.add_parser("setup", help="Enable or reconfigure the Weixin DM channel")
    _add_weixin_setup_arguments(weixin_setup)
    weixin_setup.set_defaults(handler=setup.handle_init_weixin)
    weixin_start = weixin_sub.add_parser("start", help="Start the Weixin channel in the background")
    _add_dir_argument(weixin_start)
    weixin_start.set_defaults(handler=runtime.handle_start, channels=[CHANNEL_WEIXIN])
    weixin_stop = weixin_sub.add_parser("stop", help="Stop the background Weixin channel")
    _add_dir_argument(weixin_stop)
    weixin_stop.set_defaults(handler=runtime.handle_stop, channels=[CHANNEL_WEIXIN])
    weixin_restart = weixin_sub.add_parser("restart", help="Restart the background Weixin channel")
    _add_dir_argument(weixin_restart)
    weixin_restart.set_defaults(handler=runtime.handle_restart, channels=[CHANNEL_WEIXIN])
    weixin_status = weixin_sub.add_parser("status", help="Show Weixin channel status")
    _add_dir_argument(weixin_status)
    weixin_status.add_argument("--json", action="store_true", dest="json_output", help="Print machine-readable JSON")
    weixin_status.set_defaults(handler=runtime.handle_status, channels=[CHANNEL_WEIXIN])
    weixin_logs = weixin_sub.add_parser("logs", help="Show Weixin channel logs")
    _add_dir_argument(weixin_logs)
    weixin_logs.add_argument("--lines", type=int, default=80, help="Number of trailing log lines to print")
    weixin_logs.add_argument("--follow", "-f", action="store_true", help="Follow log output")
    weixin_logs.set_defaults(handler=runtime.handle_logs, channels=[CHANNEL_WEIXIN])

    doctor_parser = subparsers.add_parser("doctor", help="Check local xAgent readiness")
    _add_dir_argument(doctor_parser)
    _add_channel_argument(doctor_parser, default_label="enabled channels")
    doctor_parser.add_argument("--online", action="store_true", help="Include network/model checks")
    doctor_parser.set_defaults(handler=runtime.handle_doctor)

    inspect_parser = subparsers.add_parser("inspect", help="Inspect configuration, identity, memory, or messages")
    inspect_sub = inspect_parser.add_subparsers(dest="inspect_target", metavar="<target>")
    inspect_sub.required = True

    config_parser = inspect_sub.add_parser("config", help="Show or validate config.yaml")
    config_sub = config_parser.add_subparsers(dest="config_command", metavar="<subcommand>")
    config_sub.required = True
    for command_name in ("show", "validate", "path"):
        config_cmd = config_sub.add_parser(command_name, help=f"{command_name} config.yaml")
        _add_dir_argument(config_cmd)
        config_cmd.set_defaults(handler=runtime.handle_config)

    identity_parser = inspect_sub.add_parser("identity", help="Show identity.md information")
    identity_sub = identity_parser.add_subparsers(dest="identity_command", metavar="<subcommand>")
    identity_sub.required = True
    for command_name in ("show", "path"):
        identity_cmd = identity_sub.add_parser(command_name, help=f"{command_name} identity.md")
        _add_dir_argument(identity_cmd)
        identity_cmd.set_defaults(handler=runtime.handle_identity)

    memory_parser = inspect_sub.add_parser("memory", help="Inspect or clear long-term daily memory")
    memory_sub = memory_parser.add_subparsers(dest="memory_command", metavar="<subcommand>")
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

    messages_parser = inspect_sub.add_parser("messages", help="Inspect or clear the message stream")
    messages_sub = messages_parser.add_subparsers(dest="messages_command", metavar="<subcommand>")
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

    version_parser = subparsers.add_parser("version", help="Show xAgent version")
    version_parser.set_defaults(handler=runtime.handle_version)

    internal_run = subparsers.add_parser("_run-channel", help=argparse.SUPPRESS)
    internal_run.add_argument("channel", choices=(CHANNEL_API, CHANNEL_FEISHU, CHANNEL_WEIXIN))
    _add_dir_argument(internal_run)
    _add_api_runtime_arguments(internal_run)
    internal_run.set_defaults(handler=runtime.handle_run_channel_internal)
    _hide_subparser_choice(subparsers, "_run-channel")

    return parser