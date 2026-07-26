"""xAgent command-line entrypoint."""
from __future__ import annotations

import logging
import sys
from typing import Optional, Sequence

from .parser import build_parser
from .setup import (
    FeishuInitSelection,
    InitResult,
    InitSelection,
    VoiceInitSelection,
    WeixinInitSelection,
    collect_feishu_init_selection_terminal_ui,
    collect_init_selection,
    collect_init_selection_terminal_ui,
    collect_voice_init_selection_terminal_ui,
    collect_weixin_init_selection_terminal_ui,
    init_agent_directory,
)
from .terminal_ui import ReturnToLauncherHome, SetupCancelled, TerminalUI, rich_terminal_available

__all__ = [
    "FeishuInitSelection",
    "InitResult",
    "InitSelection",
    "ReturnToLauncherHome",
    "SetupCancelled",
    "TerminalUI",
    "VoiceInitSelection",
    "WeixinInitSelection",
    "build_parser",
    "collect_feishu_init_selection_terminal_ui",
    "collect_init_selection",
    "collect_init_selection_terminal_ui",
    "collect_voice_init_selection_terminal_ui",
    "collect_weixin_init_selection_terminal_ui",
    "init_agent_directory",
    "main",
    "rich_terminal_available",
]


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = build_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        if rich_terminal_available():
            from .launcher import run_launcher

            return run_launcher(dispatch=main)
        parser.print_help()
        return 0
    args = parser.parse_args(arguments)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    return int(handler(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
