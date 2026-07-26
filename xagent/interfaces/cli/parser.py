"""Documented command surface for the single-Runtime architecture."""
from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from . import launcher, runtime, setup, web


CHANNEL_NAMES = ("api", "feishu", "weixin", "voice")
DELIVERY_STATUSES = (
    "pending",
    "sending",
    "delivered",
    "blocked",
    "failed",
    "unknown",
)

_TOP_LEVEL_HELP = """usage:
  xagent
  xagent <command> [options]

xAgent runs one persistent individual through one Runtime and one ordered
timeline. Use Web on a desktop, or the terminal launcher on a headless host.

Setup and navigation:
  setup                         Create the active Agent's config and identity
  launcher                      Open the headless terminal control surface
  web                           Open the desktop browser management UI

Runtime lifecycle:
  run                           Run the Runtime in the foreground
  start                         Idempotently start it in the background
  stop                          Gracefully stop it
  restart                       Gracefully replace it
  status                        Show Runtime and channel state

Interaction and operations:
  chat [message]                Chat through the Runtime
  channel list                  List hot-swappable channels
  channel setup <name>          Configure api, feishu, weixin, or voice
  channel start <name>          Enable and start one channel
  channel stop <name>           Disable and stop one channel
  channel restart <name>        Restart only one channel
  delivery list                 Inspect durable outbound deliveries
  delivery retry <id>           Explicitly retry one blocked delivery
  person list                   List people and linked channel accounts
  person link ...               Explicitly link an account to a person

Target selection:
  --agent NAME                  Use a managed Agent instead of the active one

Lifecycle rules:
  * The Runtime remains alive when every channel is disabled.
  * `stop` does not change persisted channel enabled states.
  * Starting a channel starts the Runtime first when necessary.
  * Deliveries blocked by a disabled channel are never sent until retried.

Examples:
  xagent setup
  xagent
  xagent web
  xagent start --agent mono
  xagent channel setup feishu --agent mono
  xagent channel start feishu --agent mono
  xagent delivery list --status blocked --json --agent mono
  xagent status --json

Run `xagent <command> -h` for command-specific options and examples.
"""


class XAgentArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        if self.prog == "xagent" and "invalid choice" in message:
            self.print_usage(sys.stderr)
            self.exit(2, "xagent: error: unknown command. Use 'xagent -h'.\n")
        super().error(message)

    def format_help(self) -> str:
        if self.prog == "xagent":
            return _TOP_LEVEL_HELP
        return super().format_help()


def _examples(*commands: str) -> str:
    return "Examples:\n" + "\n".join(f"  {command}" for command in commands)


def _command_parser(
    subparsers: argparse._SubParsersAction,
    name: str,
    *,
    help: str,
    description: str,
    examples: Sequence[str] = (),
) -> argparse.ArgumentParser:
    return subparsers.add_parser(
        name,
        help=help,
        description=description,
        epilog=_examples(*examples) if examples else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def _add_target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--agent",
        metavar="NAME",
        default=None,
        help="target managed Agent; defaults to the active Agent",
    )
    parser.add_argument("--config-dir", default=None, help=argparse.SUPPRESS)


def _add_runtime_command(
    subparsers: argparse._SubParsersAction,
    name: str,
    handler: Callable,
    *,
    help: str,
    description: str,
    examples: Sequence[str],
) -> argparse.ArgumentParser:
    parser = _command_parser(
        subparsers,
        name,
        help=help,
        description=description,
        examples=examples,
    )
    _add_target(parser)
    parser.set_defaults(handler=handler)
    return parser


def _add_setup_target(parser: argparse.ArgumentParser) -> None:
    _add_target(parser)
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace existing settings for this setup target",
    )


def _add_channel_setup_commands(
    channel_sub: argparse._SubParsersAction,
) -> None:
    channel_setup = _command_parser(
        channel_sub,
        "setup",
        help="configure one channel",
        description=(
            "Configure exactly one transport. Options are channel-specific; "
            "setup does not start the channel."
        ),
        examples=(
            "xagent channel setup api --host 127.0.0.1 --port 8010",
            "xagent channel setup feishu",
            "xagent channel setup weixin",
            "xagent channel setup voice --provider qwen",
        ),
    )
    setup_sub = channel_setup.add_subparsers(
        dest="name",
        title="channels",
        metavar="{api,feishu,weixin,voice}",
        required=True,
    )

    api = _command_parser(
        setup_sub,
        "api",
        help="configure the public HTTP/WebSocket channel",
        description=(
            "Configure the public API listener. This is separate from the "
            "authenticated loopback Runtime control service."
        ),
        examples=(
            "xagent channel setup api",
            "xagent channel setup api --host 0.0.0.0 --port 8010 --agent mono",
        ),
    )
    _add_target(api)
    api.add_argument(
        "--host",
        metavar="HOST",
        default=None,
        help="listen address; defaults to the configured value or 127.0.0.1",
    )
    api.add_argument(
        "--port",
        metavar="PORT",
        type=int,
        default=None,
        help="listen port; defaults to the configured value or 8010",
    )
    api.set_defaults(handler=runtime.handle_channel)

    feishu = _command_parser(
        setup_sub,
        "feishu",
        help="configure the Feishu bot channel",
        description=(
            "Configure Feishu using one-click registration or explicit app "
            "credentials. Existing settings require --force."
        ),
        examples=(
            "xagent channel setup feishu",
            "xagent channel setup feishu --manual --app-id APP_ID --app-secret APP_SECRET",
            "xagent channel setup feishu --force --agent mono",
        ),
    )
    _add_setup_target(feishu)
    feishu.add_argument(
        "--manual",
        action="store_true",
        help="enter an existing App ID and App Secret instead of one-click setup",
    )
    feishu.add_argument("--app-id", metavar="ID", default=None, help="Feishu App ID")
    feishu.add_argument(
        "--app-secret",
        metavar="SECRET",
        default=None,
        help="Feishu App Secret",
    )
    feishu.add_argument(
        "--stream",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="stream model output when the Feishu transport supports it",
    )
    feishu.add_argument(
        "--group-fetch-limit",
        metavar="COUNT",
        type=int,
        default=None,
        help="recent group messages to fetch for context; 0 disables fetching",
    )
    feishu.add_argument(
        "--group-reply-only-when-mentioned",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="reply in groups only when the Agent is mentioned",
    )
    feishu.set_defaults(handler=runtime.handle_channel)

    weixin = _command_parser(
        setup_sub,
        "weixin",
        help="configure the Weixin direct-message channel",
        description=(
            "Authenticate the Weixin iLink channel by QR code. Only direct "
            "messages are handled. Existing settings require --force."
        ),
        examples=(
            "xagent channel setup weixin",
            "xagent channel setup weixin --allow-user USER_ID",
            "xagent channel setup weixin --no-owner-only --force --agent mono",
        ),
    )
    _add_setup_target(weixin)
    weixin.add_argument(
        "--base-url",
        metavar="URL",
        default=None,
        help="iLink API base URL",
    )
    weixin.add_argument(
        "--cdn-base-url",
        metavar="URL",
        default=None,
        help="media CDN base URL",
    )
    weixin.add_argument(
        "--bot-type",
        metavar="TYPE",
        default="3",
        help="iLink bot type; default: 3",
    )
    weixin.add_argument(
        "--owner-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="restrict access to the authenticated owner; default: enabled",
    )
    weixin.add_argument(
        "--allow-user",
        metavar="USER_ID",
        action="append",
        dest="allow_users",
        default=None,
        help="allow an additional user; repeat for multiple users",
    )
    weixin.add_argument(
        "--media",
        action=argparse.BooleanOptionalAction,
        dest="media_enabled",
        default=True,
        help="enable inbound and outbound media; default: enabled",
    )
    weixin.set_defaults(handler=runtime.handle_channel)

    voice = _command_parser(
        setup_sub,
        "voice",
        help="configure the local microphone/speaker channel",
        description=(
            "Configure local speech recognition and synthesis. Choose one "
            "provider for both directions or custom providers for STT and TTS."
        ),
        examples=(
            "xagent channel setup voice --provider soniox",
            "xagent channel setup voice --provider qwen --wake",
            "xagent channel setup voice --provider custom --stt-provider soniox --tts-provider qwen",
        ),
    )
    _add_setup_target(voice)
    voice.add_argument(
        "--provider",
        choices=("soniox", "qwen", "custom"),
        default=None,
        help="voice provider mode",
    )
    voice.add_argument(
        "--api-key",
        metavar="KEY",
        default=None,
        help="shared STT/TTS key for soniox or qwen mode",
    )
    voice.add_argument(
        "--stt-provider",
        choices=("soniox", "qwen"),
        default=None,
        help="speech-to-text provider in custom mode",
    )
    voice.add_argument(
        "--stt-api-key",
        metavar="KEY",
        default=None,
        help="speech-to-text API key in custom mode",
    )
    voice.add_argument(
        "--tts-provider",
        choices=("soniox", "qwen"),
        default=None,
        help="text-to-speech provider in custom mode",
    )
    voice.add_argument(
        "--tts-api-key",
        metavar="KEY",
        default=None,
        help="text-to-speech API key in custom mode",
    )
    voice.add_argument(
        "--wake",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="require a wake phrase before accepting speech",
    )
    voice.add_argument(
        "--wake-phrase",
        metavar="TEXT",
        action="append",
        dest="wake_phrases",
        default=None,
        help="wake phrase; repeat for multiple phrases",
    )
    voice.add_argument(
        "--exit-phrase",
        metavar="TEXT",
        action="append",
        dest="exit_phrases",
        default=None,
        help="phrase that returns voice to wake-waiting mode; repeatable",
    )
    voice.add_argument(
        "--interruptions",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="allow new speech to interrupt current playback",
    )
    voice.set_defaults(handler=runtime.handle_channel)


def build_parser() -> argparse.ArgumentParser:
    parser = XAgentArgumentParser(prog="xagent", add_help=True)
    sub = parser.add_subparsers(dest="command")

    setup_parser = _command_parser(
        sub,
        "setup",
        help="create config and identity for the active Agent",
        description=(
            "Create the strict schema-version 2 configuration, identity, "
            "workspace and memory directories for one Agent."
        ),
        examples=(
            "xagent setup",
            "xagent setup --force",
        ),
    )
    _add_target(setup_parser)
    setup_parser.add_argument(
        "--force",
        action="store_true",
        help="replace existing config and identity files",
    )
    setup_parser.set_defaults(handler=setup.handle_init)

    launcher_parser = _command_parser(
        sub,
        "launcher",
        help="open the headless terminal control surface",
        description=(
            "Open the keyboard-driven terminal control surface for SSH and "
            "headless systems. Exiting it does not stop the Runtime."
        ),
        examples=(
            "xagent",
            "xagent launcher",
            "xagent launcher --agent mono",
        ),
    )
    launcher_parser.add_argument(
        "--agent",
        metavar="NAME",
        default=None,
        help="initial managed Agent; defaults to the active Agent",
    )
    launcher_parser.set_defaults(handler=launcher.handle_launcher)

    web_parser = _command_parser(
        sub,
        "web",
        help="open the desktop browser management UI",
        description=(
            "Run the desktop management center in the foreground and open it in "
            "a browser. Manage Agents, messages, memory, tasks, channels, and "
            "recovery from one place. It is unauthenticated, so it only listens "
            "on loopback. Stopping Web does not stop any Agent Runtime."
        ),
        examples=(
            "xagent web",
            "xagent web --no-open",
            "xagent web --port 8080 --agent mono",
        ),
    )
    web_parser.add_argument(
        "--agent",
        metavar="NAME",
        default=None,
        help="initial managed Agent; defaults to the active Agent",
    )
    web_parser.add_argument(
        "--host",
        metavar="HOST",
        type=web.loopback_host,
        default=web.DEFAULT_WEB_HOST,
        help="loopback listen address; default: 127.0.0.1",
    )
    web_parser.add_argument(
        "--port",
        metavar="PORT",
        type=web.web_port,
        default=web.DEFAULT_WEB_PORT,
        help="listen port; default: 1415",
    )
    web_parser.add_argument(
        "--open",
        action=argparse.BooleanOptionalAction,
        dest="open_browser",
        default=True,
        help="open the Web UI in the default browser; default: enabled",
    )
    web_parser.set_defaults(handler=web.handle_web)

    _add_runtime_command(
        sub,
        "run",
        runtime.handle_runtime_foreground,
        help="run the Runtime in the foreground",
        description=(
            "Run the single Runtime attached to this terminal. Use this for "
            "service supervisors and debugging; SIGINT/SIGTERM stop it gracefully."
        ),
        examples=("xagent run", "xagent run --agent mono"),
    )
    _add_runtime_command(
        sub,
        "start",
        runtime.handle_runtime_start,
        help="start the Runtime in the background",
        description=(
            "Idempotently start one background Runtime. If another launcher "
            "wins the process lease, this command reuses that healthy instance."
        ),
        examples=("xagent start", "xagent start --agent mono"),
    )
    _add_runtime_command(
        sub,
        "stop",
        runtime.handle_runtime_stop,
        help="gracefully stop the Runtime",
        description=(
            "Stop the whole Runtime through its authenticated control service. "
            "Persisted channel enabled states are not changed."
        ),
        examples=("xagent stop", "xagent stop --agent mono"),
    )
    _add_runtime_command(
        sub,
        "restart",
        runtime.handle_runtime_restart,
        help="gracefully replace the Runtime",
        description=(
            "Stop the current Runtime, wait for that exact instance to exit, "
            "then start a new instance."
        ),
        examples=("xagent restart", "xagent restart --agent mono"),
    )
    status = _add_runtime_command(
        sub,
        "status",
        runtime.handle_runtime_status,
        help="show Runtime and channel state",
        description=(
            "Read the authenticated local control plane. A stopped Runtime "
            "is reported normally and does not produce an error."
        ),
        examples=("xagent status", "xagent status --json --agent mono"),
    )
    status.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit machine-readable JSON",
    )

    chat = _command_parser(
        sub,
        "chat",
        help="chat through the single Runtime",
        description=(
            "Submit a chat event to the ordered Runtime timeline. Without a "
            "message, open an interactive terminal chat. The Runtime starts if needed."
        ),
        examples=(
            'xagent chat "What happened today?"',
            'xagent chat "Hello" --agent mono',
        ),
    )
    _add_target(chat)
    chat.add_argument(
        "message",
        nargs="?",
        help="one message to send; omit for interactive chat",
    )
    chat.set_defaults(handler=runtime.handle_chat)

    channel = _command_parser(
        sub,
        "channel",
        help="configure and control hot-swappable channels",
        description=(
            "Manage transport adapters independently from the Runtime. A "
            "channel failure or stop does not stop the Agent or other channels."
        ),
        examples=(
            "xagent channel list",
            "xagent channel setup feishu",
            "xagent channel start feishu",
            "xagent channel stop feishu",
        ),
    )
    channel_sub = channel.add_subparsers(
        dest="channel_action",
        title="channel commands",
        metavar="{list,setup,start,stop,restart}",
        required=True,
    )
    channel_list = _command_parser(
        channel_sub,
        "list",
        help="list channel configuration and runtime state",
        description=(
            "List every channel. When the Runtime is stopped, persisted enabled "
            "states are read directly from the strict configuration."
        ),
        examples=("xagent channel list", "xagent channel list --json --agent mono"),
    )
    _add_target(channel_list)
    channel_list.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit machine-readable JSON",
    )
    channel_list.set_defaults(handler=runtime.handle_channel)
    _add_channel_setup_commands(channel_sub)

    channel_actions = {
        "start": (
            "enable and start one channel",
            "Start the Runtime if necessary, persist enabled=true, then start only this channel.",
        ),
        "stop": (
            "disable and stop one channel",
            "Persist enabled=false and gracefully stop only this channel. New outbound work for it becomes blocked.",
        ),
        "restart": (
            "restart one channel",
            "Keep the Runtime and other channels alive while replacing only this channel.",
        ),
    }
    for action, (help_text, description) in channel_actions.items():
        action_parser = _command_parser(
            channel_sub,
            action,
            help=help_text,
            description=description,
            examples=(
                f"xagent channel {action} feishu",
                f"xagent channel {action} voice --agent mono",
            ),
        )
        action_parser.add_argument(
            "name",
            choices=CHANNEL_NAMES,
            metavar="{api,feishu,weixin,voice}",
            help="channel to control",
        )
        _add_target(action_parser)
        action_parser.set_defaults(handler=runtime.handle_channel)

    delivery = _command_parser(
        sub,
        "delivery",
        help="inspect and retry durable outbound deliveries",
        description=(
            "Inspect durable outbound operations. Only blocked deliveries may "
            "be retried; unknown deliveries require human confirmation."
        ),
        examples=(
            "xagent delivery list --status blocked",
            "xagent delivery retry DELIVERY_ID",
        ),
    )
    delivery_sub = delivery.add_subparsers(
        dest="delivery_action",
        title="delivery commands",
        metavar="{list,retry}",
        required=True,
    )
    delivery_list = _command_parser(
        delivery_sub,
        "list",
        help="list durable deliveries",
        description="List deliveries, optionally filtering by their exact persisted status.",
        examples=(
            "xagent delivery list",
            "xagent delivery list --status blocked --json",
        ),
    )
    _add_target(delivery_list)
    delivery_list.add_argument(
        "--status",
        choices=DELIVERY_STATUSES,
        default=None,
        help="filter by persisted delivery status",
    )
    delivery_list.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit machine-readable JSON",
    )
    delivery_list.set_defaults(handler=runtime.handle_delivery)
    delivery_retry = _command_parser(
        delivery_sub,
        "retry",
        help="retry one blocked delivery",
        description=(
            "Move one blocked delivery back to pending. Restoring its channel "
            "alone never sends it; this explicit command is required."
        ),
        examples=("xagent delivery retry DELIVERY_ID",),
    )
    delivery_retry.add_argument(
        "delivery_id",
        metavar="DELIVERY_ID",
        help="exact blocked delivery ID",
    )
    _add_target(delivery_retry)
    delivery_retry.set_defaults(handler=runtime.handle_delivery)

    person = _command_parser(
        sub,
        "person",
        help="inspect and link cross-channel identities",
        description=(
            "Manage explicit person/account links. xAgent never guesses that "
            "accounts on different channels belong to the same person."
        ),
        examples=(
            "xagent person list",
            "xagent person link PERSON_ID feishu ACCOUNT_ID",
        ),
    )
    person_sub = person.add_subparsers(
        dest="person_action",
        title="person commands",
        metavar="{list,link}",
        required=True,
    )
    person_list = _command_parser(
        person_sub,
        "list",
        help="list people and their linked accounts",
        description="List stable person IDs and every explicitly linked channel account.",
        examples=("xagent person list", "xagent person list --json --agent mono"),
    )
    _add_target(person_list)
    person_list.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit machine-readable JSON",
    )
    person_list.set_defaults(handler=runtime.handle_person)
    person_link = _command_parser(
        person_sub,
        "link",
        help="link one channel account to a person",
        description=(
            "Explicitly assert that one channel account belongs to an existing "
            "person ID. This affects identity resolution, not memory isolation."
        ),
        examples=(
            "xagent person link PERSON_ID feishu ou_123",
            "xagent person link PERSON_ID weixin wxid_123 --agent mono",
        ),
    )
    person_link.add_argument("person_id", metavar="PERSON_ID", help="existing person ID")
    person_link.add_argument(
        "channel",
        choices=CHANNEL_NAMES,
        metavar="{api,feishu,weixin,voice}",
        help="channel that owns the account",
    )
    person_link.add_argument(
        "account_id",
        metavar="ACCOUNT_ID",
        help="channel-native account ID",
    )
    _add_target(person_link)
    person_link.set_defaults(handler=runtime.handle_person)

    return parser
