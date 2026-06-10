"""Public CLI package and entrypoint for xAgent.

This package wires the argument parser to the command handlers and exposes the
interactive launcher. Submodules own distinct concerns:

* :mod:`.runtime` — command handlers (chat, web, start/stop, status, ...)
* :mod:`.setup` — ``init`` flows and channel onboarding
* :mod:`.launcher` — the interactive terminal launcher and config editors
* :mod:`.parser` — argparse assembly
* :mod:`.chat` — the :class:`AgentCLI` chat client
"""

from __future__ import annotations

import sys
from typing import Optional, Sequence

from .chat import (
    AgentCLI,
    _default_cli_user_id,
    _format_cli_attachments,
    _format_cli_workspace_links,
)
from .launcher import (
    _launcher_channel_options,
    _launcher_help_content,
    _launcher_options,
    _launcher_overview_subtitle,
    _run_channel_launcher,
    _run_inspect_launcher,
    _run_interactive_launcher,
    _run_model_config_launcher,
    _run_partial_update_launcher,
    _run_resetup_launcher,
)
from .parser import build_parser
from .runtime import (
    handle_chat,
    handle_config,
    handle_doctor,
    handle_identity,
    handle_logs,
    handle_memory,
    handle_messages,
    handle_observe,
    handle_restart,
    handle_run,
    handle_run_channel_internal,
    handle_server,
    handle_start,
    handle_status,
    handle_status_all,
    handle_stop,
    handle_version,
    handle_voice,
    handle_web,
    print_quick_start,
)
from .setup import (
    FeishuInitSelection,
    InitResult,
    InitSelection,
    WeixinInitSelection,
    collect_feishu_init_selection_terminal_ui,
    collect_init_selection,
    collect_init_selection_terminal_ui,
    collect_weixin_init_selection_terminal_ui,
    handle_init,
    handle_init_feishu,
    handle_init_weixin,
    init_agent_directory,
)
from .terminal_ui import ReturnToLauncherHome, TerminalUI, rich_terminal_available

__all__ = [
    "AgentCLI",
    "FeishuInitSelection",
    "InitResult",
    "InitSelection",
    "ReturnToLauncherHome",
    "TerminalUI",
    "WeixinInitSelection",
    "build_parser",
    "collect_feishu_init_selection_terminal_ui",
    "collect_init_selection",
    "collect_init_selection_terminal_ui",
    "collect_weixin_init_selection_terminal_ui",
    "handle_chat",
    "handle_config",
    "handle_doctor",
    "handle_identity",
    "handle_init",
    "handle_init_feishu",
    "handle_init_weixin",
    "handle_logs",
    "handle_memory",
    "handle_messages",
    "handle_observe",
    "handle_restart",
    "handle_run",
    "handle_run_channel_internal",
    "handle_server",
    "handle_start",
    "handle_status",
    "handle_status_all",
    "handle_stop",
    "handle_version",
    "handle_voice",
    "handle_web",
    "init_agent_directory",
    "main",
    "print_quick_start",
    "rich_terminal_available",
]


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
