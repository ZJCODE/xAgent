"""Public CLI facade and entrypoint."""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from typing import Optional, Sequence

import yaml

from ..core.runtime import create_runtime_heartbeat
from .base import BaseAgentRunner
from . import cli_launcher as _cli_launcher
from . import cli_runtime as _cli_runtime
from . import cli_setup as _cli_setup
from .cli_chat import AgentCLI, _default_cli_user_id, _format_cli_attachments, _format_cli_workspace_links
from .cli_parser import build_parser as _build_parser_impl
from .cli_runtime import (
    _launcher_args,
    _run_api_channel,
    _runtime_is_initialized,
    _xagent_version_text,
    handle_config,
    handle_doctor,
    handle_identity,
    handle_memory,
    handle_messages,
    handle_observe,
    handle_run,
    handle_server,
    handle_version,
)
from .cli_setup import (
    DEFAULT_MEMORY_LIST_DAYS,
    FeishuInitSelection,
    InitResult,
    InitSelection,
    WeixinInitSelection,
    _format_init_command,
    _print_feishu_post_setup,
    _register_feishu_app_via_qr,
    _terminal_prompt_yes_no,
    _terminal_select_model_option,
    collect_feishu_init_selection_terminal_ui,
    collect_init_selection,
    collect_init_selection_terminal_ui,
    collect_weixin_init_selection_terminal_ui,
    init_agent_directory,
)
from .overview import build_runtime_overview
from .processes import running_pid, start_background, stop_managed_process
from .terminal_ui import ReturnToLauncherHome, TerminalUI, rich_terminal_available


_launcher_channel_options = _cli_launcher._launcher_channel_options
_launcher_help_content = _cli_launcher._launcher_help_content
_launcher_options = _cli_launcher._launcher_options
_launcher_overview_subtitle = _cli_launcher._launcher_overview_subtitle
_managed_channel_actions = _cli_launcher._managed_channel_actions

_handle_init_impl = _cli_setup.handle_init
_handle_init_feishu_impl = _cli_setup.handle_init_feishu
_handle_init_weixin_impl = _cli_setup.handle_init_weixin
_collect_feishu_init_selection_terminal_ui_impl = _cli_setup.collect_feishu_init_selection_terminal_ui
_collect_weixin_init_selection_terminal_ui_impl = _cli_setup.collect_weixin_init_selection_terminal_ui

_run_model_config_launcher_impl = _cli_launcher._run_model_config_launcher
_run_observability_config_launcher_impl = _cli_launcher._run_observability_config_launcher
_run_voice_provider_mode_launcher_impl = _cli_launcher._run_voice_provider_mode_launcher
_apply_config_update_impl = _cli_launcher._apply_config_update
_required_feature_api_key_impl = _cli_launcher._required_feature_api_key
_run_partial_update_launcher_impl = _cli_launcher._run_partial_update_launcher
_run_resetup_launcher_impl = _cli_launcher._run_resetup_launcher
_run_channel_launcher_impl = _cli_launcher._run_channel_launcher
_run_inspect_launcher_impl = _cli_launcher._run_inspect_launcher
_run_interactive_launcher_impl = _cli_launcher._run_interactive_launcher
_run_voice_wake_config_launcher_impl = _cli_launcher._run_voice_wake_config_launcher
_run_voice_config_launcher_impl = _cli_launcher._run_voice_config_launcher
_run_voice_channel_launcher_impl = _cli_launcher._run_voice_channel_launcher
_run_voice_nested_config_impl = _cli_launcher._run_voice_nested_config
_print_quick_start_impl = _cli_runtime.print_quick_start


def _sync_setup_module() -> None:
    _cli_setup.TerminalUI = TerminalUI
    _cli_setup.ReturnToLauncherHome = ReturnToLauncherHome
    _cli_setup.getpass = getpass
    _cli_setup.collect_init_selection_terminal_ui = collect_init_selection_terminal_ui
    _cli_setup.collect_feishu_init_selection_terminal_ui = collect_feishu_init_selection_terminal_ui
    _cli_setup.collect_weixin_init_selection_terminal_ui = collect_weixin_init_selection_terminal_ui
    _cli_setup._terminal_prompt_yes_no = _terminal_prompt_yes_no
    _cli_setup._print_feishu_post_setup = _print_feishu_post_setup
    _cli_setup._register_feishu_app_via_qr = _register_feishu_app_via_qr


def _sync_runtime_module() -> None:
    _cli_runtime.BaseAgentRunner = BaseAgentRunner
    _cli_runtime.create_runtime_heartbeat = create_runtime_heartbeat
    _cli_runtime.start_background = start_background
    _cli_runtime.stop_managed_process = stop_managed_process
    _cli_runtime.running_pid = running_pid
    _cli_runtime._run_api_channel = _run_api_channel


def _sync_launcher_module() -> None:
    _cli_launcher.TerminalUI = TerminalUI
    _cli_launcher.ReturnToLauncherHome = ReturnToLauncherHome
    _cli_launcher.handle_chat = handle_chat
    _cli_launcher.handle_config = handle_config
    _cli_launcher.handle_identity = handle_identity
    _cli_launcher.handle_init = handle_init
    _cli_launcher.handle_init_feishu = handle_init_feishu
    _cli_launcher.handle_init_weixin = handle_init_weixin
    _cli_launcher.handle_messages = handle_messages
    _cli_launcher.handle_start = handle_start
    _cli_launcher.handle_status = handle_status
    _cli_launcher.handle_stop = handle_stop
    _cli_launcher.handle_voice = handle_voice
    _cli_launcher.handle_web = handle_web
    _cli_launcher.build_runtime_overview = build_runtime_overview
    _cli_launcher._launcher_overview_subtitle = _launcher_overview_subtitle
    _cli_launcher._run_partial_update_launcher = _run_partial_update_launcher
    _cli_launcher._run_resetup_launcher = _run_resetup_launcher
    _cli_launcher._run_model_config_launcher = _run_model_config_launcher
    _cli_launcher._run_observability_config_launcher = _run_observability_config_launcher
    _cli_launcher._run_voice_provider_mode_launcher = _run_voice_provider_mode_launcher
    _cli_launcher._apply_config_update = _apply_config_update
    _cli_launcher._required_feature_api_key = _required_feature_api_key
    _cli_launcher._terminal_select_model_option = _terminal_select_model_option


def handle_chat(args):
    return _cli_runtime.handle_chat(args)


def handle_voice(args):
    return _cli_runtime.handle_voice(args)


def handle_web(args):
    _sync_runtime_module()
    return _cli_runtime.handle_web(args)


def handle_start(args):
    _sync_runtime_module()
    return _cli_runtime.handle_start(args)


def handle_stop(args):
    _sync_runtime_module()
    return _cli_runtime.handle_stop(args)


def handle_restart(args):
    _sync_runtime_module()
    return _cli_runtime.handle_restart(args)


def handle_status(args):
    _sync_runtime_module()
    return _cli_runtime.handle_status(args)


def handle_logs(args):
    _sync_runtime_module()
    return _cli_runtime.handle_logs(args)


def print_quick_start() -> None:
    _cli_runtime._runtime_is_initialized = _runtime_is_initialized
    _print_quick_start_impl()


def handle_run_channel_internal(args):
    _sync_runtime_module()
    return _cli_runtime.handle_run_channel_internal(args)


def handle_init(args):
    _sync_setup_module()
    return _handle_init_impl(args)


def handle_init_feishu(args):
    _sync_setup_module()
    return _handle_init_feishu_impl(args)


def handle_init_weixin(args):
    _sync_setup_module()
    return _handle_init_weixin_impl(args)


def collect_feishu_init_selection_terminal_ui(*, args, ui=None, input_func=None, secret_input_func=None):
    _sync_setup_module()
    return _collect_feishu_init_selection_terminal_ui_impl(
        args=args,
        ui=ui,
        input_func=input_func,
        secret_input_func=secret_input_func,
    )


def collect_weixin_init_selection_terminal_ui(*, args, ui=None):
    _sync_setup_module()
    return _collect_weixin_init_selection_terminal_ui_impl(args=args, ui=ui)


def _run_model_config_launcher(ui, config_dir):
    _sync_launcher_module()
    return _run_model_config_launcher_impl(ui, config_dir)


def _run_observability_config_launcher(ui, config_dir):
    _sync_launcher_module()
    return _run_observability_config_launcher_impl(ui, config_dir)


def _run_voice_provider_mode_launcher(ui, config_dir, config):
    _sync_launcher_module()
    return _run_voice_provider_mode_launcher_impl(ui, config_dir, config)


def _run_voice_nested_config(ui, config_dir, config, section):
    _sync_launcher_module()
    return _run_voice_nested_config_impl(ui, config_dir, config, section)


def _run_voice_wake_config_launcher(ui, config_dir):
    _sync_launcher_module()
    return _run_voice_wake_config_launcher_impl(ui, config_dir)


def _run_voice_config_launcher(ui, config_dir):
    _sync_launcher_module()
    return _run_voice_config_launcher_impl(ui, config_dir)


def _run_voice_channel_launcher(ui, config_dir):
    _sync_launcher_module()
    return _run_voice_channel_launcher_impl(ui, config_dir)


def _apply_config_update(ui, config_dir, update, *, return_home_on_success=False):
    return _apply_config_update_impl(
        ui,
        config_dir,
        update,
        return_home_on_success=return_home_on_success,
    )


def _required_feature_api_key(ui, *, provider, feature):
    return _required_feature_api_key_impl(ui, provider=provider, feature=feature)


def _run_partial_update_launcher(ui, config_dir):
    _sync_launcher_module()
    return _run_partial_update_launcher_impl(ui, config_dir)


def _run_resetup_launcher(config_dir):
    _sync_launcher_module()
    return _run_resetup_launcher_impl(config_dir)


def _run_channel_launcher(config_dir):
    _sync_launcher_module()
    return _run_channel_launcher_impl(config_dir)


def _run_inspect_launcher(config_dir):
    _sync_launcher_module()
    return _run_inspect_launcher_impl(config_dir)


def _run_interactive_launcher():
    _sync_launcher_module()
    return _run_interactive_launcher_impl()


def build_parser() -> argparse.ArgumentParser:
    return _build_parser_impl(sys.modules[__name__])


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