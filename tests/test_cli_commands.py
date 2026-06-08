import argparse
import asyncio
import io
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import yaml

from xagent.interfaces.channels import enabled_channels_from_config
from xagent.interfaces.cli import (
    AgentCLI,
    FeishuInitSelection,
    InitSelection,
    _run_inspect_launcher,
    _run_interactive_launcher,
    _run_channel_launcher,
    _run_partial_update_launcher,
    _format_cli_attachments,
    _format_cli_workspace_links,
    _launcher_channel_options,
    _launcher_help_content,
    _launcher_options,
    build_parser,
    collect_feishu_init_selection_terminal_ui,
    handle_config,
    handle_chat,
    handle_init,
    handle_init_feishu,
    handle_logs,
    handle_memory,
    handle_observe,
    handle_restart,
    handle_run_channel_internal,
    handle_start,
    handle_status,
    handle_stop,
    handle_voice,
    handle_web,
    main,
)
from xagent.interfaces.config_editor import (
    prepare_image_generation_provider_update,
    prepare_model_provider_update,
    prepare_observability_update,
    prepare_search_provider_update,
    prepare_voice_interruptions_update,
    prepare_voice_wake_update,
    write_config,
)
from xagent.interfaces.overview import STATUS_ERROR, build_runtime_overview
from xagent.interfaces.processes import StartResult


def _selection(**overrides) -> InitSelection:
    values = {
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key": "test-key",
        "model": "gpt-5.4-mini",
        "identity": "# Identity\n\nTest agent.\n",
        "search_provider": "openai",
    }
    values.update(overrides)
    return InitSelection(
        **values,
    )


def _write_runtime(directory: str, *, feishu: bool = False) -> None:
    config = {
        "provider": {
            "name": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key": "test-key",
            "model": "gpt-5.4-mini",
        },
        "search": {"provider": "openai"},
        "channels": {
            "api": {
                "host": "127.0.0.1",
                "port": 8010,
            }
        },
    }
    if feishu:
        config["channels"]["feishu"] = {
            "app_id": "cli_test",
            "app_secret": "secret",
        }
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (root / "identity.md").write_text("# Identity\n\nTest agent.\n", encoding="utf-8")


class CLICommandTests(unittest.TestCase):
    def test_cli_formats_workspace_blob_links_as_local_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve() / "workspace"
            source = "![Generated image](/api/workspace/blob?path=temp%2Fimages%2Fresult.png)"

            formatted = _format_cli_workspace_links(source, workspace)

        self.assertEqual(formatted, f"![Generated image]({workspace / 'temp' / 'images' / 'result.png'})")

    def test_cli_keeps_workspace_blob_traversal_urls_unchanged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve() / "workspace"
            source = "![Generated image](/api/workspace/blob?path=..%2Fresult.png)"

            formatted = _format_cli_workspace_links(source, workspace)

        self.assertEqual(formatted, source)

    def test_cli_formats_structured_attachments_as_local_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve() / "workspace"
            attachments = [{
                "kind": "image",
                "path": "temp/images/result.png",
                "blob_url": "/api/workspace/blob?path=temp%2Fimages%2Fresult.png",
                "mime_type": "image/png",
            }]

            formatted = _format_cli_attachments(attachments, workspace)

        self.assertEqual(formatted, f"Attachments:\n- {workspace / 'temp' / 'images' / 'result.png'}")

    def test_inspect_launcher_message_list_accepts_custom_count(self):
        class FakeUI:
            def __init__(self):
                self.inspect_choices = iter([
                    SimpleNamespace(key="message"),
                    SimpleNamespace(key="back"),
                ])
                self.message_choices = iter([
                    SimpleNamespace(key="list"),
                    SimpleNamespace(key="back"),
                ])
                self.count_option_keys = []
                self.inspect_option_titles = []

            def select_menu(self, *, title, subtitle, options, footer):
                del subtitle, footer
                if title == "xAgent Inspect":
                    self.inspect_option_titles = [option.title for option in options]
                    return next(self.inspect_choices)
                if title == "xAgent Inspect / Message":
                    return next(self.message_choices)
                raise AssertionError(f"Unexpected menu: {title}")

            def select(self, *, label, subtitle="", options, default_index=0):
                del subtitle, default_index
                if label != "Recent message count":
                    raise AssertionError(f"Unexpected select prompt: {label}")
                self.count_option_keys = [option.key for option in options]
                return SimpleNamespace(key="custom")

            def ask_text(self, label, *, default=None, secret=False, subtitle=""):
                del default, secret, subtitle
                if label != "Recent message count":
                    raise AssertionError(f"Unexpected text prompt: {label}")
                return "7"

            def clear(self):
                return None

            def pause(self, message="Press Enter to continue"):
                del message
                return None

            def print_panel(self, *args, **kwargs):
                raise AssertionError("No error panel expected")

        fake_ui = FakeUI()

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("xagent.interfaces.cli.TerminalUI", return_value=fake_ui):
                with patch("xagent.interfaces.cli.handle_messages", return_value=0) as handle_messages:
                    exit_code = _run_inspect_launcher(Path(tmpdir))

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            fake_ui.inspect_option_titles,
            ["Config", "Identity", "Memory", "Message", "Skills", "Tasks", "Back"],
        )
        self.assertEqual(fake_ui.count_option_keys, ["2", "5", "10", "custom"])
        handle_messages.assert_called_once()
        args = handle_messages.call_args.args[0]
        self.assertEqual(args.messages_command, "list")
        self.assertEqual(args.count, 7)
        self.assertEqual(args.offset, 0)

    def test_chat_events_print_structured_attachment_paths(self):
        class FakeAgent:
            async def chat_events(self, **kwargs):
                yield {"type": "message_start"}
                yield {
                    "type": "message_done",
                    "content": "",
                    "attachments": [{
                        "kind": "image",
                        "path": "temp/images/result.png",
                        "blob_url": "/api/workspace/blob?path=temp%2Fimages%2Fresult.png",
                        "mime_type": "image/png",
                    }],
                }
                yield {"type": "done"}

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir).resolve() / "workspace"
            cli = AgentCLI.__new__(AgentCLI)
            cli.agent = FakeAgent()
            cli.workspace_dir = workspace

            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                asyncio.run(cli._print_chat_events(
                    user_message="draw",
                    user_id="alice",
                    stream=False,
                    enable_memory=True,
                ))

        output = stdout.getvalue()
        self.assertIn(str(workspace / "temp" / "images" / "result.png"), output)
        self.assertIn("Attachments:", output)
        self.assertNotIn("/api/workspace/blob", output)

    def test_parser_supports_init_schema_command(self):
        args = build_parser().parse_args([
            "init",
            "--dir",
            "./agent-dir",
            "--schema",
        ])

        self.assertEqual(args.command, "init")
        self.assertEqual(args.config_dir, "./agent-dir")
        self.assertTrue(args.schema)

    def test_parser_supports_init_feishu_command(self):
        args = build_parser().parse_args([
            "init",
            "feishu",
            "--dir",
            "./agent-dir",
            "--app-id",
            "cli_test",
            "--app-secret",
            "secret",
            "--force",
        ])

        self.assertEqual(args.command, "init")
        self.assertEqual(args.init_target, "feishu")
        self.assertEqual(args.config_dir, "./agent-dir")
        self.assertEqual(args.app_id, "cli_test")
        self.assertTrue(args.force)

    def test_parser_supports_init_feishu_runtime_options(self):
        args = build_parser().parse_args([
            "init",
            "feishu",
            "--stream",
            "--no-memory",
            "--group-history-count",
            "20",
            "--show-sender-ids",
            "--group-reply-without-mention",
        ])

        self.assertTrue(args.stream)
        self.assertFalse(args.enable_memory)
        self.assertEqual(args.group_history_count, 20)
        self.assertTrue(args.show_sender_ids)
        self.assertTrue(args.group_reply_without_mention)

    def test_parser_supports_chat_message_command(self):
        args = build_parser().parse_args([
            "chat",
            "Hello",
            "--dir",
            "./agent-dir",
            "--user-id",
            "alice",
            "--no-memory",
        ])

        self.assertEqual(args.command, "chat")
        self.assertEqual(args.message, "Hello")
        self.assertEqual(args.config_dir, "./agent-dir")
        self.assertEqual(args.user_id, "alice")
        self.assertFalse(args.memory)

    def test_parser_supports_chat_event_mode(self):
        args = build_parser().parse_args([
            "chat",
            "Hello",
            "--events",
            "--stream",
        ])

        self.assertTrue(args.events)
        self.assertTrue(args.stream)

    def test_parser_supports_voice_command(self):
        args = build_parser().parse_args([
            "voice",
            "--dir",
            "./agent-dir",
            "--user-id",
            "alice",
            "--no-memory",
        ])

        self.assertEqual(args.command, "voice")
        self.assertEqual(args.config_dir, "./agent-dir")
        self.assertEqual(args.user_id, "alice")
        self.assertFalse(args.memory)

    def test_voice_command_runs_foreground_runtime(self):
        class FakeAgent:
            model = "gpt-test"
            tools = {}

            def __init__(self):
                self.flush_count = 0

            async def flush_memory(self):
                self.flush_count += 1

        class FakeRuntime:
            def __init__(self):
                self.run_count = 0

            async def run_forever(self):
                self.run_count += 1

        fake_agent = FakeAgent()
        fake_runtime = FakeRuntime()

        def init_runner(self, config_dir=None):
            self.agent = fake_agent
            self.config = {
                "channels": {
                    "voice": {
                        "provider": "soniox",
                        "stt": {
                            "provider": "soniox",
                            "api_key": "soniox-key",
                        },
                        "tts": {
                            "provider": "soniox",
                            "api_key": "soniox-key",
                        },
                    }
                }
            }

        with tempfile.TemporaryDirectory() as tmpdir:
            args = argparse.Namespace(
                config_dir=tmpdir,
                user_id="alice",
                verbose=False,
                memory=False,
            )

            with patch("xagent.interfaces.cli.BaseAgentRunner.__init__", init_runner):
                with patch("xagent.voice.factory.create_local_voice_runtime", return_value=fake_runtime) as factory:
                    with patch("sys.stdout", new_callable=io.StringIO):
                        exit_code = handle_voice(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(fake_runtime.run_count, 1)
        self.assertEqual(fake_agent.flush_count, 0)
        self.assertFalse(factory.call_args.kwargs["options"].enable_memory)
        self.assertEqual(factory.call_args.kwargs["options"].user_id, "alice")

    def test_interactive_chat_exit_does_not_flush_memory(self):
        class FakeAgent:
            model = "gpt-test"
            tools = {}

            def __init__(self):
                self.flush_count = 0

            async def flush_memory(self):
                self.flush_count += 1

        fake_agent = FakeAgent()

        def init_runner(self, config_dir=None):
            self.agent = fake_agent
            self.message_storage = SimpleNamespace()
            self.config_dir = Path(config_dir)
            self.config_path = self.config_dir / "config.yaml"

        with tempfile.TemporaryDirectory() as tmpdir:
            args = argparse.Namespace(
                message=None,
                config_dir=tmpdir,
                user_id=None,
                verbose=False,
                stream=None,
                events=False,
                memory=True,
            )

            with patch("xagent.interfaces.cli.BaseAgentRunner.__init__", init_runner):
                with patch("builtins.input", return_value="bye"):
                    with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                        exit_code = handle_chat(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(fake_agent.flush_count, 0)
        output = stdout.getvalue()
        self.assertIn("Thank you for using xAgent CLI", output)
        self.assertNotIn("正在写入退出前记忆", output)

    def test_interactive_chat_uses_simple_you_prompt(self):
        class FakeAgent:
            model = "gpt-test"
            tools = {}

        cli = AgentCLI.__new__(AgentCLI)
        cli.agent = FakeAgent()
        cli.message_storage = SimpleNamespace()
        cli.config_dir = Path("/tmp/xagent")
        cli.config_path = cli.config_dir / "config.yaml"

        fake_ui = MagicMock()
        fake_ui.input.return_value = "bye"

        with patch("xagent.interfaces.cli.TerminalUI", return_value=fake_ui):
            asyncio.run(cli._chat_interactive_terminal_ui(
                user_id="alice",
                stream=False,
                memory=True,
                verbose_mode=False,
            ))

        fake_ui.input.assert_called_once_with("[bold cyan]You:[/bold cyan] ")

    def test_single_chat_does_not_flush_memory(self):
        class FakeAgent:
            model = "gpt-test"
            tools = {}

            def __init__(self):
                self.flush_count = 0
                self.call_kwargs = None

            async def __call__(self, **kwargs):
                self.call_kwargs = kwargs
                return "single reply"

            async def flush_memory(self):
                self.flush_count += 1

        fake_agent = FakeAgent()

        def init_runner(self, config_dir=None):
            self.agent = fake_agent
            self.message_storage = SimpleNamespace()
            self.config_dir = Path(config_dir)
            self.config_path = self.config_dir / "config.yaml"

        with tempfile.TemporaryDirectory() as tmpdir:
            args = argparse.Namespace(
                message="Hello",
                config_dir=tmpdir,
                user_id="alice",
                verbose=False,
                stream=None,
                events=False,
                memory=True,
            )

            with patch("xagent.interfaces.cli.BaseAgentRunner.__init__", init_runner):
                with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    exit_code = handle_chat(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(fake_agent.flush_count, 0)
        self.assertEqual(fake_agent.call_kwargs["user_id"], "alice")
        self.assertNotIn("stream", fake_agent.call_kwargs)
        output = stdout.getvalue()
        self.assertIn("single reply", output)
        self.assertNotIn("正在写入退出前记忆", output)

    def test_parser_supports_web_command(self):
        args = build_parser().parse_args([
            "web",
            "--dir",
            "./agent-dir",
            "--no-open",
            "--host",
            "127.0.0.1",
            "--port",
            "8010",
            "--max-concurrent-chats",
            "2",
            "--queue-timeout",
            "3.5",
            "--chat-timeout",
            "9.5",
        ])

        self.assertEqual(args.command, "web")
        self.assertEqual(args.config_dir, "./agent-dir")
        self.assertFalse(args.open_browser)
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8010)
        self.assertEqual(args.max_concurrent_chats, 2)
        self.assertEqual(args.queue_timeout, 3.5)
        self.assertEqual(args.chat_timeout, 9.5)

    def test_parser_supports_channel_lifecycle_commands(self):
        args = build_parser().parse_args([
            "channel",
            "api",
            "start",
            "--dir",
            "./agent-dir",
            "--host",
            "127.0.0.1",
            "--port",
            "8010",
        ])

        self.assertEqual(args.command, "channel")
        self.assertEqual(args.channel_target, "api")
        self.assertEqual(args.channel_action, "start")
        self.assertEqual(args.channels, ["api"])
        self.assertEqual(args.config_dir, "./agent-dir")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8010)

        feishu = build_parser().parse_args(["channel", "feishu", "logs", "--follow"])
        self.assertEqual(feishu.command, "channel")
        self.assertEqual(feishu.channel_target, "feishu")
        self.assertEqual(feishu.channel_action, "logs")
        self.assertEqual(feishu.channels, ["feishu"])
        self.assertTrue(feishu.follow)

    def test_service_command_is_removed(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["service", "start", "api"])

    def test_parser_supports_observe_and_inspect_commands(self):
        observe = build_parser().parse_args([
            "observe",
            "ambient context",
            "--source",
            "sensor",
            "--event-type",
            "room_state",
        ])
        self.assertEqual(observe.command, "observe")
        self.assertEqual(observe.source, "sensor")

        config = build_parser().parse_args(["inspect", "config", "validate", "--dir", "./agent-dir"])
        self.assertEqual(config.config_command, "validate")

        memory = build_parser().parse_args(["inspect", "memory", "search", "project", "--scope", "daily"])
        self.assertEqual(memory.memory_command, "search")
        self.assertEqual(memory.query, "project")

        memory_list = build_parser().parse_args(["inspect", "memory", "list", "--days", "7"])
        self.assertEqual(memory_list.memory_command, "list")
        self.assertEqual(memory_list.days, 7)

        with self.assertRaises(SystemExit):
            build_parser().parse_args(["inspect", "memory", "show", "daily/2026/2026-06/2026-06-07.md"])

        messages = build_parser().parse_args(["inspect", "messages", "list", "--count", "5"])
        self.assertEqual(messages.messages_command, "list")
        self.assertEqual(messages.count, 5)

    def test_memory_list_prints_recent_daily_journals(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            memory_root = root / "memory"
            today = date.today()
            recent = memory_root / "daily" / str(today.year) / f"{today.year}-{today.month:02d}" / f"{today.isoformat()}.md"
            recent.parent.mkdir(parents=True, exist_ok=True)
            recent.write_text("## 09:00\n\nRecent note\n", encoding="utf-8")
            old_day = today - timedelta(days=9)
            old = memory_root / "daily" / str(old_day.year) / f"{old_day.year}-{old_day.month:02d}" / f"{old_day.isoformat()}.md"
            old.parent.mkdir(parents=True, exist_ok=True)
            old.write_text("## 09:00\n\nOld note\n", encoding="utf-8")

            args = argparse.Namespace(config_dir=tmpdir, memory_command="list", days=7)
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                exit_code = handle_memory(args)

        self.assertEqual(exit_code, 0)
        output = stdout.getvalue()
        self.assertIn("Recent note", output)
        self.assertNotIn("Old note", output)

    def test_observe_does_not_flush_memory_on_exit(self):
        class FakeAgent:
            def __init__(self):
                self.observe_kwargs = None
                self.flush_count = 0

            async def observe(self, **kwargs):
                self.observe_kwargs = kwargs
                return "observed"

            async def flush_memory(self):
                self.flush_count += 1

        fake_agent = FakeAgent()

        def init_runner(self, config_dir=None):
            self.agent = fake_agent

        with tempfile.TemporaryDirectory() as tmpdir:
            args = argparse.Namespace(
                text="ambient context",
                source="sensor",
                event_type="presence",
                metadata='{"memory_policy":"always"}',
                config_dir=tmpdir,
            )

            with patch("xagent.interfaces.cli.BaseAgentRunner.__init__", init_runner):
                with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    exit_code = handle_observe(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(fake_agent.flush_count, 0)
        self.assertEqual(fake_agent.observe_kwargs["context"], "ambient context")
        self.assertEqual(fake_agent.observe_kwargs["metadata"], {"memory_policy": "always"})
        self.assertIn("observed", stdout.getvalue())

    def test_main_without_subcommand_prints_quick_start(self):
        with patch("xagent.interfaces.cli._runtime_is_initialized", return_value=False):
            with patch("sys.stdout") as stdout:
                exit_code = main([])

        self.assertEqual(exit_code, 0)
        output = "".join(call.args[0] for call in stdout.write.call_args_list if call.args)
        self.assertIn("Quick start", output)
        self.assertIn("init", output)
        self.assertIn("chat", output)
        self.assertIn("web", output)

    def test_main_uses_interactive_launcher_when_terminal_ui_is_available(self):
        with patch("xagent.interfaces.cli.rich_terminal_available", return_value=True):
            with patch("xagent.interfaces.cli._run_interactive_launcher", return_value=0) as launcher:
                exit_code = main([])

        self.assertEqual(exit_code, 0)
        launcher.assert_called_once_with()

    def test_launcher_options_use_resetup_after_initial_setup(self):
        initial_options = _launcher_options(initialized=False)
        reset_options = _launcher_options(initialized=True)

        self.assertEqual(initial_options[0].title, "Setup")
        self.assertEqual(reset_options[0].title, "Resetup")
        self.assertIn("force", reset_options[0].description.lower())
        reset_titles = [option.title for option in reset_options]
        self.assertIn("Help", reset_titles)
        self.assertNotIn("Doctor", reset_titles)
        self.assertNotIn("Version", reset_titles)

    def test_launcher_channel_options_are_entry_points(self):
        options = _launcher_channel_options()
        titles = [option.title for option in options]

        self.assertEqual(titles, ["Chat", "Voice", "Web", "Feishu", "Back"])
        self.assertNotIn("All", titles)

    def test_channel_launcher_start_chooses_channel_before_action(self):
        class FakeUI:
            def __init__(self):
                self.channel_choices = iter([
                    SimpleNamespace(key="api", title="Web"),
                    SimpleNamespace(key="back"),
                ])
                self.channel_option_titles = []
                self.action_choices = iter([SimpleNamespace(key="start")])
                self.action_option_titles = []

            def select_menu(self, *, title, subtitle, options, footer):
                del subtitle, footer
                if title == "xAgent Channel":
                    self.channel_option_titles = [option.title for option in options]
                    return next(self.channel_choices)
                if title == "xAgent Channel / Web":
                    self.action_option_titles = [option.title for option in options]
                    return next(self.action_choices)
                raise AssertionError(f"Unexpected menu: {title}")

            def clear(self):
                return None

            def pause(self, message="Press Enter to continue"):
                del message
                return None

            def print_panel(self, *args, **kwargs):
                raise AssertionError("No error panel expected")

        fake_ui = FakeUI()

        with patch("xagent.interfaces.cli.TerminalUI", return_value=fake_ui):
            with patch("xagent.interfaces.cli.handle_start", return_value=0) as starter:
                exit_code = _run_channel_launcher(Path("/tmp/xagent"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(fake_ui.channel_option_titles[:4], ["Chat", "Voice", "Web", "Feishu"])
        self.assertIn("Start Background", fake_ui.action_option_titles)
        self.assertIn("Open Web UI", fake_ui.action_option_titles)
        self.assertNotIn("Start API", fake_ui.action_option_titles)
        starter.assert_called_once()
        args = starter.call_args.args[0]
        self.assertEqual(args.channels, ["api"])
        self.assertEqual(args.config_dir, "/tmp/xagent")

    def test_channel_launcher_feishu_setup_runs_init_feishu_when_missing(self):
        class FakeUI:
            def __init__(self):
                self.channel_choices = iter([
                    SimpleNamespace(key="feishu", title="Feishu"),
                    SimpleNamespace(key="back"),
                ])
                self.action_choices = iter([SimpleNamespace(key="setup")])
                self.action_option_titles = []

            def select_menu(self, *, title, subtitle, options, footer):
                del subtitle, footer
                if title == "xAgent Channel":
                    return next(self.channel_choices)
                if title == "xAgent Channel / Feishu":
                    self.action_option_titles = [option.title for option in options]
                    return next(self.action_choices)
                raise AssertionError(f"Unexpected menu: {title}")

            def clear(self):
                return None

            def pause(self, message="Press Enter to continue"):
                del message
                return None

            def print_panel(self, *args, **kwargs):
                raise AssertionError("No error panel expected")

        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir)
            fake_ui = FakeUI()

            with patch("xagent.interfaces.cli.TerminalUI", return_value=fake_ui):
                with patch("xagent.interfaces.cli.handle_init_feishu", return_value=0) as init_feishu:
                    exit_code = _run_channel_launcher(Path(tmpdir))

        self.assertEqual(exit_code, 0)
        self.assertEqual(fake_ui.action_option_titles[0], "Setup")
        init_feishu.assert_called_once()
        args = init_feishu.call_args.args[0]
        self.assertEqual(args.config_dir, tmpdir)
        self.assertFalse(args.force)

    def test_channel_launcher_feishu_hides_setup_when_configured(self):
        class FakeUI:
            def __init__(self):
                self.channel_choices = iter([
                    SimpleNamespace(key="feishu", title="Feishu"),
                    SimpleNamespace(key="back"),
                ])
                self.action_choices = iter([SimpleNamespace(key="status")])
                self.action_option_titles = []

            def select_menu(self, *, title, subtitle, options, footer):
                del subtitle, footer
                if title == "xAgent Channel":
                    return next(self.channel_choices)
                if title == "xAgent Channel / Feishu":
                    self.action_option_titles = [option.title for option in options]
                    return next(self.action_choices)
                raise AssertionError(f"Unexpected menu: {title}")

            def clear(self):
                return None

            def pause(self, message="Press Enter to continue"):
                del message
                return None

            def print_panel(self, *args, **kwargs):
                raise AssertionError("No error panel expected")

        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir, feishu=True)
            fake_ui = FakeUI()

            with patch("xagent.interfaces.cli.TerminalUI", return_value=fake_ui):
                with patch("xagent.interfaces.cli.handle_status", return_value=0) as status:
                    exit_code = _run_channel_launcher(Path(tmpdir))

        self.assertEqual(exit_code, 0)
        self.assertNotIn("Setup", fake_ui.action_option_titles)
        status.assert_called_once()
        args = status.call_args.args[0]
        self.assertEqual(args.config_dir, tmpdir)
        self.assertEqual(args.channels, ["feishu"])

    def test_runtime_overview_flags_search_missing_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir)
            config_path = Path(tmpdir) / "config.yaml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["search"] = {"provider": "qwen"}
            config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

            overview = build_runtime_overview(Path(tmpdir))

        search = next(item for item in overview.items if item.name == "Search")
        self.assertEqual(search.status, STATUS_ERROR)
        self.assertEqual(search.detail, "Set search.api_key")

    def test_runtime_overview_uses_friendly_idle_copy_for_stopped_web_ui(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir)

            overview = build_runtime_overview(Path(tmpdir))

        self.assertEqual(overview.headline, "Ready")
        web_ui = next(item for item in overview.items if item.name == "Web UI")
        self.assertEqual(web_ui.status, "idle")
        self.assertEqual(web_ui.value, "ready to start")
        self.assertEqual(web_ui.detail, "Open at http://127.0.0.1:8010 after launch")
        image = next(item for item in overview.items if item.name == "Image")
        self.assertEqual(image.value, "not enabled")
        self.assertEqual(image.detail, "Set image_generation.provider to enable")
        voice = next(item for item in overview.items if item.name == "Voice")
        self.assertEqual(voice.value, "not enabled")
        self.assertEqual(voice.detail, "Set channels.voice to enable voice mode")

    def test_runtime_overview_shows_web_ui_url_when_running(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir)
            with patch("xagent.interfaces.overview.running_pid", return_value=26807):
                overview = build_runtime_overview(Path(tmpdir))

        web_ui = next(item for item in overview.items if item.name == "Web UI")
        self.assertEqual(web_ui.status, "ok")
        self.assertEqual(web_ui.value, "running")
        self.assertEqual(web_ui.detail, "Open http://127.0.0.1:8010  pid 26807")

    def test_config_editor_updates_search_provider_with_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir)
            root = Path(tmpdir)
            config = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))

            update = prepare_search_provider_update(config, provider="qwen", api_key="qwen-key")
            write_config(root, update.data)
            saved = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))

        self.assertEqual(saved["search"]["provider"], "qwen")
        self.assertEqual(saved["search"]["api_key"], "qwen-key")
        self.assertIn("search.provider", [change.path for change in update.changes])

    def test_config_editor_updates_model_provider_and_feature_keys(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir)
            root = Path(tmpdir)
            config = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
            config["image_generation"] = {"provider": "openai"}

            update = prepare_model_provider_update(
                config,
                provider="qwen",
                model="qwen3.6-flash",
                api_key="qwen-key",
                search_api_key="openai-search-key",
                image_generation_api_key="openai-image-key",
            )
            write_config(root, update.data)
            saved = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))

        self.assertEqual(saved["provider"]["name"], "qwen")
        self.assertEqual(saved["provider"]["model"], "qwen3.6-flash")
        self.assertEqual(saved["search"]["api_key"], "openai-search-key")
        self.assertEqual(saved["image_generation"]["api_key"], "openai-image-key")

    def test_config_editor_updates_image_generation_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir)
            root = Path(tmpdir)
            config = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))

            update = prepare_image_generation_provider_update(config, provider="qwen", api_key="qwen-image-key")
            write_config(root, update.data)
            saved = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))

        self.assertEqual(saved["image_generation"]["provider"], "qwen")
        self.assertEqual(saved["image_generation"]["api_key"], "qwen-image-key")
        self.assertEqual(saved["image_generation"]["model"], "qwen-image-2.0-pro")

    def test_config_editor_updates_observability(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir)
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

            update = prepare_observability_update(
                config,
                enabled=True,
                public_key="pk-lf-test",
                secret_key="sk-lf-test",
                base_url="https://us.cloud.langfuse.com",
            )
            write_config(root, update.data)
            saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        self.assertEqual(
            saved["observability"],
            {
                "enabled": True,
                "provider": "langfuse",
                "public_key": "pk-lf-test",
                "secret_key": "sk-lf-test",
                "base_url": "https://us.cloud.langfuse.com",
            },
        )
        self.assertIn("observability.enabled", [change.path for change in update.changes])
        change_map = {change.path: change for change in update.changes}
        self.assertEqual(change_map["observability.secret_key"].after, "(secret)")

    def test_config_editor_rejects_observability_for_anthropic_model_api(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir)
            root = Path(tmpdir)
            config = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
            config["provider"]["name"] = "anthropic"
            config["provider"]["base_url"] = "https://api.anthropic.com"
            config["provider"]["model"] = "claude-sonnet-4-20250514"

            with self.assertRaisesRegex(ValueError, "OpenAI-compatible model API"):
                prepare_observability_update(
                    config,
                    enabled=True,
                    public_key="pk-lf-test",
                    secret_key="sk-lf-test",
                )

    def test_config_editor_updates_voice_wake_and_interruptions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir)
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["channels"]["voice"] = {
                "provider": "qwen",
                "enable_interruptions": False,
                "stt": {"api_key": "qwen-key"},
                "tts": {"api_key": "qwen-key"},
                "wake": {"enabled": False, "wake_phrases": ["xAgent"], "exit_phrases": ["exit"]},
            }

            update = prepare_voice_interruptions_update(config, enabled=True)
            update = prepare_voice_wake_update(
                update.data,
                enabled=True,
                wake_phrases=["hey xagent", "assistant"],
                exit_phrases=["stop", "done"],
                match_mode="contains",
                idle_timeout_seconds=120,
            )
            write_config(root, update.data)
            saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        self.assertTrue(saved["channels"]["voice"]["enable_interruptions"])
        self.assertTrue(saved["channels"]["voice"]["wake"]["enabled"])
        self.assertEqual(saved["channels"]["voice"]["wake"]["wake_phrases"], ["hey xagent", "assistant"])
        self.assertEqual(saved["channels"]["voice"]["wake"]["exit_phrases"], ["stop", "done"])
        self.assertEqual(saved["channels"]["voice"]["wake"]["match_mode"], "contains")
        self.assertEqual(saved["channels"]["voice"]["wake"]["idle_timeout_seconds"], 120.0)

    def test_partial_update_launcher_includes_observability_and_routes(self):
        class FakeUI:
            def __init__(self):
                self.choices = iter([
                    SimpleNamespace(key="observability"),
                    SimpleNamespace(key="back"),
                ])
                self.option_titles = []

            def select_menu(self, *, title, subtitle, options, footer):
                del subtitle, footer
                if title != "xAgent Resetup / Partial Update":
                    raise AssertionError(f"Unexpected menu: {title}")
                self.option_titles = [option.title for option in options]
                return next(self.choices)

            def clear(self):
                return None

            def pause(self, message="Press Enter to continue"):
                del message
                return None

            def print_panel(self, *args, **kwargs):
                raise AssertionError("No error panel expected")

        fake_ui = FakeUI()

        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir)
            config_dir = Path(tmpdir)

            with patch("xagent.interfaces.cli._run_observability_config_launcher") as observability_launcher:
                _run_partial_update_launcher(fake_ui, config_dir)

        self.assertEqual(
            fake_ui.option_titles,
            ["Model", "Observability", "Search", "Feishu", "Voice", "Image Generation", "Back"],
        )
        observability_launcher.assert_called_once_with(fake_ui, config_dir)

    def test_interactive_launcher_resetup_opens_resetup_menu(self):
        class FakeUI:
            def __init__(self):
                self.choices = iter([
                    SimpleNamespace(key="init", disabled=False),
                    SimpleNamespace(key="exit", disabled=False),
                ])
                self.option_titles = []

            def select_menu(self, *, title, subtitle, options, footer):
                del title, subtitle, footer
                if not self.option_titles:
                    self.option_titles = [option.title for option in options]
                return next(self.choices)

            def clear(self):
                return None

            def pause(self, message="Press Enter to continue"):
                del message
                return None

            def print_panel(self, *args, **kwargs):
                raise AssertionError("No error panel expected")

        fake_ui = FakeUI()

        with patch("xagent.interfaces.cli.TerminalUI", return_value=fake_ui):
            with patch("xagent.interfaces.cli._runtime_is_initialized", return_value=True):
                with patch("xagent.interfaces.cli._run_resetup_launcher", return_value=0) as resetup_launcher:
                    exit_code = _run_interactive_launcher()

        self.assertEqual(exit_code, 0)
        self.assertIn("Resetup", fake_ui.option_titles)
        resetup_launcher.assert_called_once()

    def test_interactive_launcher_help_prints_command_guide(self):
        class FakeUI:
            def __init__(self):
                self.choices = iter([
                    SimpleNamespace(key="help", disabled=False),
                    SimpleNamespace(key="exit", disabled=False),
                ])
                self.panels = []

            def select_menu(self, *, title, subtitle, options, footer):
                del title, subtitle, options, footer
                return next(self.choices)

            def clear(self):
                return None

            def pause(self, message="Press Enter to continue"):
                del message
                return None

            def print_panel(self, message, *, title=None, **kwargs):
                del kwargs
                self.panels.append((title, str(message)))

        fake_ui = FakeUI()

        with patch("xagent.interfaces.cli.TerminalUI", return_value=fake_ui):
            with patch("xagent.interfaces.cli._runtime_is_initialized", return_value=True):
                exit_code = _run_interactive_launcher()

        self.assertEqual(exit_code, 0)
        self.assertEqual(fake_ui.panels[0][0], "xAgent Help")
        self.assertIn("xagent init --force", fake_ui.panels[0][1])
        self.assertIn("xagent channel api start", fake_ui.panels[0][1])
        self.assertIn("xagent inspect memory list --days 7", fake_ui.panels[0][1])
        self.assertNotIn("xagent doctor", fake_ui.panels[0][1])

    def test_launcher_help_content_switches_setup_command_by_state(self):
        config_dir = Path("/tmp/xagent")

        setup_help = str(_launcher_help_content(config_dir=config_dir, initialized=False))
        resetup_help = str(_launcher_help_content(config_dir=config_dir, initialized=True))

        self.assertIn("xagent init --dir /tmp/xagent", setup_help)
        self.assertIn("xagent init --force --dir /tmp/xagent", resetup_help)

    def test_root_help_groups_public_commands(self):
        help_text = build_parser().format_help()

        self.assertIn("Start here:", help_text)
        self.assertIn("Runtime:", help_text)
        self.assertIn("Advanced:", help_text)
        self.assertIn("  web", help_text)
        self.assertIn("  channel", help_text)
        self.assertNotIn("  service", help_text)
        self.assertNotIn("  run", help_text)
        self.assertNotIn("  start", help_text)

    def test_old_top_level_commands_are_rejected(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["start"])

    def test_init_force_can_keep_runtime_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "config.yaml").write_text("provider:\n  model: old\n", encoding="utf-8")
            (root / "identity.md").write_text("old", encoding="utf-8")
            memory_marker = root / "memory" / "entry.md"
            messages_marker = root / "messages" / "messages.sqlite3"
            workspace_marker = root / "workspace" / "notes.md"
            tasks_marker = root / "tasks" / "task.json"
            skills_marker = root / "skills" / "demo" / "SKILL.md"
            memory_marker.parent.mkdir()
            messages_marker.parent.mkdir()
            workspace_marker.parent.mkdir()
            tasks_marker.parent.mkdir()
            skills_marker.parent.mkdir(parents=True)
            memory_marker.write_text("keep-memory", encoding="utf-8")
            messages_marker.write_text("keep-messages", encoding="utf-8")
            workspace_marker.write_text("keep-workspace", encoding="utf-8")
            tasks_marker.write_text("keep-task", encoding="utf-8")
            skills_marker.write_text("keep-skill", encoding="utf-8")
            args = argparse.Namespace(config_dir=tmpdir, force=True, schema=False)

            with patch("xagent.interfaces.cli._terminal_prompt_yes_no", return_value=False) as prompt:
                with patch("xagent.interfaces.cli.collect_init_selection_terminal_ui", return_value=_selection()):
                    exit_code = handle_init(args)

            self.assertEqual(exit_code, 0)
            prompt.assert_called_once_with(
                unittest.mock.ANY,
                "Clear existing memory/, messages/, workspace/, tasks/, and skills/ data as part of init --force?",
                default=False,
            )
            self.assertEqual(memory_marker.read_text(encoding="utf-8"), "keep-memory")
            self.assertEqual(messages_marker.read_text(encoding="utf-8"), "keep-messages")
            self.assertEqual(workspace_marker.read_text(encoding="utf-8"), "keep-workspace")
            self.assertEqual(tasks_marker.read_text(encoding="utf-8"), "keep-task")
            self.assertEqual(skills_marker.read_text(encoding="utf-8"), "keep-skill")

    def test_init_force_can_clear_runtime_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "config.yaml").write_text("provider:\n  model: old\n", encoding="utf-8")
            (root / "identity.md").write_text("old", encoding="utf-8")
            memory_marker = root / "memory" / "entry.md"
            messages_marker = root / "messages" / "messages.sqlite3"
            workspace_marker = root / "workspace" / "notes.md"
            tasks_marker = root / "tasks" / "task.json"
            skills_marker = root / "skills" / "demo" / "SKILL.md"
            memory_marker.parent.mkdir()
            messages_marker.parent.mkdir()
            workspace_marker.parent.mkdir()
            tasks_marker.parent.mkdir()
            skills_marker.parent.mkdir(parents=True)
            memory_marker.write_text("clear-memory", encoding="utf-8")
            messages_marker.write_text("clear-messages", encoding="utf-8")
            workspace_marker.write_text("clear-workspace", encoding="utf-8")
            tasks_marker.write_text("clear-task", encoding="utf-8")
            skills_marker.write_text("clear-skill", encoding="utf-8")
            args = argparse.Namespace(config_dir=tmpdir, force=True, schema=False)

            with patch("xagent.interfaces.cli._terminal_prompt_yes_no", return_value=True) as prompt:
                with patch("xagent.interfaces.cli.collect_init_selection_terminal_ui", return_value=_selection()):
                    exit_code = handle_init(args)

            self.assertEqual(exit_code, 0)
            prompt.assert_called_once_with(
                unittest.mock.ANY,
                "Clear existing memory/, messages/, workspace/, tasks/, and skills/ data as part of init --force?",
                default=False,
            )
            self.assertTrue((root / "memory").is_dir())
            self.assertTrue((root / "messages").is_dir())
            self.assertTrue((root / "workspace").is_dir())
            self.assertTrue((root / "tasks").is_dir())
            self.assertTrue((root / "skills").is_dir())
            self.assertFalse(memory_marker.exists())
            self.assertFalse(messages_marker.exists())
            self.assertFalse(workspace_marker.exists())
            self.assertFalse(tasks_marker.exists())
            self.assertFalse(skills_marker.exists())

    def test_init_prints_post_setup_guide_with_custom_dir_commands(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            args = argparse.Namespace(config_dir=tmpdir, force=False, schema=False)
            resolved_dir = str(Path(tmpdir).resolve())

            with patch("xagent.interfaces.cli.collect_init_selection_terminal_ui", return_value=_selection()):
                with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    exit_code = handle_init(args)

        self.assertEqual(exit_code, 0)
        output = stdout.getvalue()
        self.assertIn("Pick how you want to use it next", output)
        self.assertIn(f"xagent chat --dir {resolved_dir}", output)
        self.assertIn(f"xagent web --dir {resolved_dir}", output)
        self.assertIn(f"xagent channel api start --dir {resolved_dir}", output)
        self.assertIn(f"xagent channel feishu setup --dir {resolved_dir}", output)
        self.assertIn(f"xagent channel feishu start --dir {resolved_dir}", output)
        self.assertNotIn("xagent doctor", output)
        self.assertNotIn(f"xagent voice --dir {resolved_dir}", output)

    def test_init_prints_voice_entry_when_voice_is_configured(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            args = argparse.Namespace(config_dir=tmpdir, force=False, schema=False)
            resolved_dir = str(Path(tmpdir).resolve())

            with patch(
                "xagent.interfaces.cli.collect_init_selection_terminal_ui",
                return_value=_selection(
                    voice_enabled=True,
                    voice_provider="qwen",
                    voice_api_key="voice-key",
                ),
            ):
                with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    exit_code = handle_init(args)

        self.assertEqual(exit_code, 0)
        output = stdout.getvalue()
        self.assertIn(f"xagent voice --dir {resolved_dir}", output)

    def test_init_uses_terminal_wizard(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            args = argparse.Namespace(config_dir=tmpdir, force=False, schema=False)

            with patch("xagent.interfaces.cli.TerminalUI") as terminal_ui:
                with patch(
                    "xagent.interfaces.cli.collect_init_selection_terminal_ui",
                    return_value=_selection(),
                ) as wizard:
                    exit_code = handle_init(args)

        self.assertEqual(exit_code, 0)
        wizard.assert_called_once_with(ui=terminal_ui.return_value)

    def test_init_feishu_uses_terminal_wizard(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir)
            args = argparse.Namespace(
                config_dir=tmpdir,
                app_id=None,
                app_secret=None,
                manual=False,
                force=False,
            )
            selection = FeishuInitSelection(app_id="cli_test", app_secret="secret")

            with patch("xagent.interfaces.cli.TerminalUI") as terminal_ui:
                with patch(
                    "xagent.interfaces.cli.collect_feishu_init_selection_terminal_ui",
                    return_value=selection,
                ) as wizard:
                    with patch("xagent.interfaces.cli._print_feishu_post_setup"):
                        exit_code = handle_init_feishu(args)

        self.assertEqual(exit_code, 0)
        wizard.assert_called_once_with(args=args, ui=terminal_ui.return_value)

    def test_init_feishu_updates_unified_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir)
            args = argparse.Namespace(
                config_dir=tmpdir,
                app_id=None,
                app_secret=None,
                manual=True,
                force=False,
                stream=None,
                enable_memory=None,
                group_history_count=None,
                show_sender_ids=None,
                group_reply_without_mention=None,
            )

            with patch("builtins.input", return_value="cli_test") as input_mock:
                with patch("xagent.interfaces.cli.getpass.getpass", return_value="secret") as getpass_mock:
                    with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                        exit_code = handle_init_feishu(args)

            config = yaml.safe_load((Path(tmpdir) / "config.yaml").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        input_mock.assert_called_once_with("Feishu App ID: ")
        getpass_mock.assert_called_once_with("Feishu App Secret: ")
        self.assertEqual(config["channels"]["feishu"]["app_id"], "cli_test")
        self.assertNotIn("enabled", config["channels"]["feishu"])
        self.assertNotIn("log_level", config["channels"]["feishu"])
        self.assertIs(config["channels"]["feishu"]["stream"], False)
        self.assertIs(config["channels"]["feishu"]["group_reply_without_mention"], False)
        self.assertNotIn("show_sender_ids", config["channels"]["feishu"])
        self.assertNotIn("runtime", config)
        output = stdout.getvalue()
        self.assertIn("xagent channel feishu start", output)

    def test_init_feishu_wizard_selection_writes_runtime_options(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir)
            args = argparse.Namespace(
                config_dir=tmpdir,
                app_id=None,
                app_secret=None,
                manual=False,
                force=False,
                stream=None,
                enable_memory=None,
                group_history_count=None,
                show_sender_ids=None,
                group_reply_without_mention=None,
            )
            selection = FeishuInitSelection(
                app_id="cli_room",
                app_secret="room_secret",
                stream=True,
                enable_memory=False,
                group_history_count=20,
                show_sender_ids=True,
                group_reply_without_mention=True,
                credential_mode="manual",
            )

            with patch(
                "xagent.interfaces.cli.collect_feishu_init_selection_terminal_ui",
                return_value=selection,
            ):
                with patch("xagent.interfaces.cli._print_feishu_post_setup"):
                    exit_code = handle_init_feishu(args)

            config = yaml.safe_load((Path(tmpdir) / "config.yaml").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(config["channels"]["feishu"]["app_id"], "cli_room")
        self.assertEqual(config["channels"]["feishu"]["app_secret"], "room_secret")
        self.assertIs(config["channels"]["feishu"]["stream"], True)
        self.assertIs(config["channels"]["feishu"]["enable_memory"], False)
        self.assertEqual(config["channels"]["feishu"]["group_history_count"], 20)
        self.assertIs(config["channels"]["feishu"]["show_sender_ids"], True)
        self.assertIs(config["channels"]["feishu"]["group_reply_without_mention"], True)

    def test_init_feishu_one_click_writes_registered_credentials(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir)
            args = argparse.Namespace(
                config_dir=tmpdir,
                app_id=None,
                app_secret=None,
                manual=False,
                force=False,
                stream=None,
                enable_memory=None,
                group_history_count=None,
                show_sender_ids=None,
                group_reply_without_mention=None,
            )

            with patch(
                "xagent.interfaces.cli._register_feishu_app_via_qr",
                return_value=("cli_qr_app", "qr_secret"),
            ) as register_mock:
                with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    exit_code = handle_init_feishu(args)

            config = yaml.safe_load((Path(tmpdir) / "config.yaml").read_text(encoding="utf-8"))
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0)
        register_mock.assert_called_once_with()
        self.assertEqual(config["channels"]["feishu"]["app_id"], "cli_qr_app")
        self.assertEqual(config["channels"]["feishu"]["app_secret"], "qr_secret")
        self.assertIs(config["channels"]["feishu"]["stream"], False)
        self.assertIs(config["channels"]["feishu"]["group_reply_without_mention"], False)
        self.assertIn("Feishu Ready", output)
        self.assertIn("Optional before group rollout", output)
        self.assertIn("If you only need direct chats right now", output)
        self.assertIn("xagent channel feishu start", output)

    def test_feishu_wizard_interactive_defaults_skip_optional_questions(self):
        class FakeUI:
            interactive = True

            def __init__(self):
                self.select_labels = []
                self.records = []

            def select(self, *, label, subtitle="", options, default_index=0):
                self.select_labels.append(label)
                if label == "App Access":
                    return SimpleNamespace(key="one_click")
                if label == "Group Routing":
                    return SimpleNamespace(key="mentions")
                raise AssertionError(f"Unexpected wizard step: {label}")

            def record(self, label, value, *, skipped=False):
                self.records.append((label, value, skipped))

            def ask_text(self, *args, **kwargs):
                raise AssertionError("Optional text questions should be skipped")

            def ask_secret(self, *args, **kwargs):
                raise AssertionError("Optional secret questions should be skipped")

            def print_panel(self, *args, **kwargs):
                raise AssertionError("No extra confirmation panel should be shown")

            def confirm(self, *args, **kwargs):
                raise AssertionError("No confirmation question should be shown")

        args = argparse.Namespace(
            config_dir=".",
            app_id=None,
            app_secret=None,
            manual=False,
            force=False,
            stream=None,
            enable_memory=None,
            group_history_count=None,
            show_sender_ids=None,
            group_reply_without_mention=None,
        )
        ui = FakeUI()

        with patch("xagent.interfaces.cli._register_feishu_app_via_qr", return_value=("cli_qr_app", "qr_secret")):
            selection = collect_feishu_init_selection_terminal_ui(args=args, ui=ui)

        self.assertEqual(ui.select_labels, ["App Access", "Group Routing"])
        self.assertEqual(selection.app_id, "cli_qr_app")
        self.assertEqual(selection.app_secret, "qr_secret")
        self.assertIs(selection.stream, False)
        self.assertIs(selection.enable_memory, True)
        self.assertEqual(selection.group_history_count, 10)
        self.assertIs(selection.show_sender_ids, False)
        self.assertIs(selection.group_reply_without_mention, False)
        self.assertEqual(selection.credential_mode, "one_click")

    def test_init_feishu_one_click_cancelled_leaves_config_untouched(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir)
            args = argparse.Namespace(
                config_dir=tmpdir,
                app_id=None,
                app_secret=None,
                manual=False,
                force=False,
                stream=None,
                enable_memory=None,
                group_history_count=None,
                show_sender_ids=None,
                group_reply_without_mention=None,
            )

            with patch("xagent.interfaces.cli._register_feishu_app_via_qr", return_value=None):
                with patch("sys.stdout"):
                    exit_code = handle_init_feishu(args)

            config = yaml.safe_load((Path(tmpdir) / "config.yaml").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertNotIn("feishu", config.get("channels", {}))

    def test_init_feishu_explicit_credentials_skip_qr_flow(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir)
            args = argparse.Namespace(
                config_dir=tmpdir,
                app_id="cli_explicit",
                app_secret="explicit_secret",
                manual=False,
                force=False,
                stream=None,
                enable_memory=None,
                group_history_count=None,
                show_sender_ids=None,
                group_reply_without_mention=None,
            )

            with patch("xagent.interfaces.cli._register_feishu_app_via_qr") as register_mock:
                with patch("sys.stdout"):
                    exit_code = handle_init_feishu(args)

            config = yaml.safe_load((Path(tmpdir) / "config.yaml").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        register_mock.assert_not_called()
        self.assertEqual(config["channels"]["feishu"]["app_id"], "cli_explicit")
        self.assertEqual(config["channels"]["feishu"]["app_secret"], "explicit_secret")

    def test_register_feishu_app_via_qr_formats_link_payload_and_returns_credentials(self):
        from xagent.interfaces.cli import _register_feishu_app_via_qr

        def fake_register_app(*, on_qr_code, on_status_change, source, cancel_event):
            on_qr_code({
                "url": "https://open.feishu.cn/page/launcher?user_code=Z9YC-ZV4A&from=sdk&tp=sdk",
                "expire_in": 3600,
            })
            on_status_change({"status": "polling"})
            return {"client_id": "cli_reg", "client_secret": "reg_secret", "user_info": {"name": "Admin"}}

        with patch("lark_oapi.register_app", side_effect=fake_register_app):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                result = _register_feishu_app_via_qr()

        output = stdout.getvalue()

        self.assertEqual(result, ("cli_reg", "reg_secret"))
        self.assertIn("Click this link to authorize", output)
        self.assertIn("https://open.feishu.cn/page/launcher?user_code=Z9YC-ZV4A&from=sdk&tp=sdk", output)
        # self.assertIn("Verification code: Z9YC-ZV4A", output)
        # self.assertIn("Link expires in: 60 minutes", output)
        self.assertIn("Waiting for authorization...", output)
        self.assertNotIn("{'url':", output)
        # QR code should either be shown or installation tip provided
        qr_or_tip = "Scan this QR code" in output or "Install qrcode" in output
        self.assertTrue(qr_or_tip, "Should display either QR code or installation tip")

    def test_register_feishu_app_via_qr_handles_access_denied(self):
        from xagent.interfaces.cli import _register_feishu_app_via_qr
        from lark_oapi.scene.registration import AppAccessDeniedError

        def fake_register_app(**_kwargs):
            raise AppAccessDeniedError("access_denied", "admin rejected")

        with patch("lark_oapi.register_app", side_effect=fake_register_app):
            with patch("sys.stdout"):
                result = _register_feishu_app_via_qr()

        self.assertIsNone(result)

    def test_run_channel_feishu_ignores_enabled_runtime_flag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir, feishu=True)
            args = argparse.Namespace(channel="feishu", config_dir=tmpdir)

            class _Runner:
                def __init__(self):
                    self.agent = SimpleNamespace(model="gpt-5.4-mini", flush_memory=self.flush_memory)

                async def flush_memory(self):
                    return None

            adapter_instance = MagicMock()
            adapter_instance.run = AsyncMock()

            with patch("xagent.interfaces.cli.BaseAgentRunner", return_value=_Runner()):
                with patch("xagent.integrations.feishu.FeishuAdapter", return_value=adapter_instance):
                    exit_code = handle_run_channel_internal(args)

        self.assertEqual(exit_code, 0)
        adapter_instance.run.assert_awaited_once_with()

    def test_start_all_includes_feishu_when_credentials_exist_without_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir, feishu=True)
            config = yaml.safe_load((Path(tmpdir) / "config.yaml").read_text(encoding="utf-8"))

        self.assertEqual(enabled_channels_from_config(config), ["api", "feishu"])

    def test_enabled_channels_do_not_implicitly_add_api_when_channels_are_explicit(self):
        config = {
            "channels": {
                "feishu": {
                    "app_id": "cli_test",
                    "app_secret": "secret",
                }
            }
        }

        self.assertEqual(enabled_channels_from_config(config), ["feishu"])

    def test_web_runs_api_channel_and_opens_browser_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir, feishu=True)
            args = argparse.Namespace(
                config_dir=tmpdir,
                host=None,
                port=None,
                open_browser=True,
                max_concurrent_chats=None,
                queue_timeout=None,
                chat_timeout=None,
            )

            with patch("xagent.interfaces.cli._run_api_channel", return_value=0) as runner:
                exit_code = handle_web(args)

        self.assertEqual(exit_code, 0)
        runner.assert_called_once()
        self.assertTrue(runner.call_args.args[0].open_browser)

    def test_start_defaults_to_feishu_when_only_feishu_is_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir, feishu=True)
            config_path = Path(tmpdir) / "config.yaml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            del config["channels"]["api"]
            config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
            args = argparse.Namespace(
                config_dir=tmpdir,
                channels=None,
                host=None,
                port=None,
                open_browser=False,
                max_concurrent_chats=None,
                queue_timeout=None,
                chat_timeout=None,
            )

            with patch("xagent.interfaces.cli.start_background", return_value=StartResult(ok=True, pid=4321)) as starter:
                exit_code = handle_start(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(starter.call_count, 1)
        self.assertEqual(starter.call_args.kwargs["pid_path"], Path(tmpdir).resolve() / "run" / "feishu.pid")

    def test_start_fails_when_no_channel_is_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir)
            config_path = Path(tmpdir) / "config.yaml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["channels"]["api"]["enabled"] = False
            config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
            args = argparse.Namespace(
                config_dir=tmpdir,
                channels=None,
                host=None,
                port=None,
                open_browser=False,
                max_concurrent_chats=None,
                queue_timeout=None,
                chat_timeout=None,
            )

            with patch("xagent.interfaces.cli.start_background") as starter:
                exit_code = handle_start(args)

        self.assertEqual(exit_code, 1)
        starter.assert_not_called()

    def test_run_channel_api_passes_options_to_server(self):
        args = argparse.Namespace(
            channel="api",
            config_dir="./agent-dir",
            host="127.0.0.1",
            port=8010,
            open_browser=True,
            max_concurrent_chats=2,
            queue_timeout=3.5,
            chat_timeout=9.5,
        )
        server_instance = MagicMock()
        server_instance.agent.model = "gpt-5.4-mini"

        with patch("xagent.interfaces.server.AgentHTTPServer", return_value=server_instance) as server_class:
            exit_code = handle_run_channel_internal(args)

        self.assertEqual(exit_code, 0)
        server_class.assert_called_once_with(
            config_dir="./agent-dir",
            max_concurrent_chats=2,
            chat_queue_timeout=3.5,
            chat_timeout=9.5,
        )
        server_instance.run.assert_called_once_with(
            host="127.0.0.1",
            port=8010,
            open_browser=True,
        )

    def test_run_channel_api_ignores_legacy_web_ui_flag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir)
            config_path = Path(tmpdir) / "config.yaml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["channels"]["api"]["web_ui"] = False
            config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
            args = argparse.Namespace(
                channel="api",
                config_dir=tmpdir,
                host=None,
                port=None,
                open_browser=True,
                max_concurrent_chats=None,
                queue_timeout=None,
                chat_timeout=None,
            )
            server_instance = MagicMock()
            server_instance.agent.model = "gpt-5.4-mini"

            with patch("xagent.interfaces.server.AgentHTTPServer", return_value=server_instance) as server_class:
                exit_code = handle_run_channel_internal(args)

        self.assertEqual(exit_code, 0)
        server_class.assert_called_once_with(config_dir=tmpdir)
        server_instance.run.assert_called_once_with(
            host="127.0.0.1",
            port=8010,
            open_browser=True,
        )

    def test_start_uses_background_processes_for_channels(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir, feishu=True)
            args = argparse.Namespace(
                config_dir=tmpdir,
                channels=["api,feishu"],
                host=None,
                port=None,
                open_browser=False,
                max_concurrent_chats=None,
                queue_timeout=None,
                chat_timeout=None,
            )

            with patch("xagent.interfaces.cli.start_background", return_value=StartResult(ok=True, pid=4321)) as starter:
                exit_code = handle_start(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(starter.call_count, 2)
        self.assertEqual(starter.call_args_list[0].kwargs["pid_path"], Path(tmpdir).resolve() / "run" / "api.pid")
        self.assertEqual(starter.call_args_list[0].kwargs["log_path"], Path(tmpdir).resolve() / "logs" / "api.log")

    def test_stop_uses_managed_pid_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir, feishu=True)
            args = argparse.Namespace(config_dir=tmpdir, channels=["api"])

            with patch("xagent.interfaces.cli.stop_managed_process", return_value=(True, "stopped")) as stopper:
                exit_code = handle_stop(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(stopper.call_args.args[0], Path(tmpdir).resolve() / "run" / "api.pid")

    def test_restart_defaults_to_auto_channel(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir, feishu=True)
            args = argparse.Namespace(
                config_dir=tmpdir,
                channels=None,
                host=None,
                port=None,
                open_browser=False,
                max_concurrent_chats=None,
                queue_timeout=None,
                chat_timeout=None,
            )

            with patch("xagent.interfaces.cli.stop_managed_process", return_value=(True, "stopped")) as stopper:
                with patch("xagent.interfaces.cli.start_background", return_value=StartResult(ok=True, pid=4321)) as starter:
                    exit_code = handle_restart(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(stopper.call_count, 1)
        self.assertEqual(starter.call_count, 1)
        self.assertEqual(stopper.call_args.args[0], Path(tmpdir).resolve() / "run" / "api.pid")

    def test_status_reports_running_process(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir, feishu=True)
            args = argparse.Namespace(config_dir=tmpdir, channels=["feishu"], json_output=False)

            with patch("xagent.interfaces.cli.running_pid", return_value=4321):
                with patch("sys.stdout") as stdout:
                    exit_code = handle_status(args)

        self.assertEqual(exit_code, 0)
        output = "".join(call.args[0] for call in stdout.write.call_args_list if call.args)
        self.assertIn("feishu: running pid=4321", output)
        self.assertIn("run/feishu.pid", output)

    def test_logs_follow_requires_explicit_single_channel(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir)
            args = argparse.Namespace(config_dir=tmpdir, channels=None, lines=10, follow=True)

            with patch("sys.stdout") as stdout:
                exit_code = handle_logs(args)

        self.assertEqual(exit_code, 1)
        output = "".join(call.args[0] for call in stdout.write.call_args_list if call.args)
        self.assertIn("--follow requires an explicit single channel", output)

    def test_logs_follow_rejects_all_channel_selector(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir)
            args = argparse.Namespace(config_dir=tmpdir, channels=["all"], lines=10, follow=True)

            with patch("sys.stdout") as stdout:
                exit_code = handle_logs(args)

        self.assertEqual(exit_code, 1)
        output = "".join(call.args[0] for call in stdout.write.call_args_list if call.args)
        self.assertIn("--follow requires an explicit single channel", output)

    def test_unknown_channel_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir)
            args = argparse.Namespace(
                config_dir=tmpdir,
                channels=["custom"],
                host=None,
                port=None,
                open_browser=False,
                max_concurrent_chats=None,
                queue_timeout=None,
                chat_timeout=None,
            )

            with patch("sys.stdout") as stdout:
                exit_code = handle_start(args)

        self.assertEqual(exit_code, 1)
        output = "".join(call.args[0] for call in stdout.write.call_args_list if call.args)
        self.assertIn("Unknown channel 'custom'", output)
        self.assertIn("api", output)
        self.assertIn("feishu", output)


if __name__ == "__main__":
    unittest.main()
