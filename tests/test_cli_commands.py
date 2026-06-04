import argparse
import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import yaml

from xagent.interfaces.channels import enabled_channels_from_config
from xagent.interfaces.cli import (
    AgentCLI,
    InitSelection,
    _format_cli_attachments,
    _format_cli_workspace_links,
    build_parser,
    handle_config,
    handle_chat,
    handle_init,
    handle_init_feishu,
    handle_logs,
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
                        "api_key": "soniox-key",
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

    def test_parser_supports_service_lifecycle_commands(self):
        args = build_parser().parse_args([
            "service",
            "start",
            "api",
            "--dir",
            "./agent-dir",
            "--host",
            "127.0.0.1",
            "--port",
            "8010",
        ])

        self.assertEqual(args.command, "service")
        self.assertEqual(args.service_command, "start")
        self.assertEqual(args.channels, "api")
        self.assertEqual(args.config_dir, "./agent-dir")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8010)

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

        messages = build_parser().parse_args(["inspect", "messages", "list", "--count", "5"])
        self.assertEqual(messages.messages_command, "list")
        self.assertEqual(messages.count, 5)

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

    def test_root_help_groups_public_commands(self):
        help_text = build_parser().format_help()

        self.assertIn("Start here:", help_text)
        self.assertIn("Runtime:", help_text)
        self.assertIn("Advanced:", help_text)
        self.assertIn("  web", help_text)
        self.assertIn("  service", help_text)
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
            memory_marker.parent.mkdir()
            messages_marker.parent.mkdir()
            workspace_marker.parent.mkdir()
            memory_marker.write_text("keep-memory", encoding="utf-8")
            messages_marker.write_text("keep-messages", encoding="utf-8")
            workspace_marker.write_text("keep-workspace", encoding="utf-8")
            args = argparse.Namespace(config_dir=tmpdir, force=True, schema=False)

            with patch("xagent.interfaces.cli._prompt_yes_no", return_value=False) as prompt:
                with patch("xagent.interfaces.cli.collect_init_selection", return_value=_selection()):
                    exit_code = handle_init(args)

            self.assertEqual(exit_code, 0)
            prompt.assert_called_once_with(
                "Clear existing memory/, messages/, and workspace/ data as part of init --force?",
                default=False,
            )
            self.assertEqual(memory_marker.read_text(encoding="utf-8"), "keep-memory")
            self.assertEqual(messages_marker.read_text(encoding="utf-8"), "keep-messages")
            self.assertEqual(workspace_marker.read_text(encoding="utf-8"), "keep-workspace")

    def test_init_force_can_clear_runtime_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "config.yaml").write_text("provider:\n  model: old\n", encoding="utf-8")
            (root / "identity.md").write_text("old", encoding="utf-8")
            memory_marker = root / "memory" / "entry.md"
            messages_marker = root / "messages" / "messages.sqlite3"
            workspace_marker = root / "workspace" / "notes.md"
            memory_marker.parent.mkdir()
            messages_marker.parent.mkdir()
            workspace_marker.parent.mkdir()
            memory_marker.write_text("clear-memory", encoding="utf-8")
            messages_marker.write_text("clear-messages", encoding="utf-8")
            workspace_marker.write_text("clear-workspace", encoding="utf-8")
            args = argparse.Namespace(config_dir=tmpdir, force=True, schema=False)

            with patch("xagent.interfaces.cli._prompt_yes_no", return_value=True) as prompt:
                with patch("xagent.interfaces.cli.collect_init_selection", return_value=_selection()):
                    exit_code = handle_init(args)

            self.assertEqual(exit_code, 0)
            prompt.assert_called_once_with(
                "Clear existing memory/, messages/, and workspace/ data as part of init --force?",
                default=False,
            )
            self.assertTrue((root / "memory").is_dir())
            self.assertTrue((root / "messages").is_dir())
            self.assertTrue((root / "workspace").is_dir())
            self.assertFalse(memory_marker.exists())
            self.assertFalse(messages_marker.exists())
            self.assertFalse(workspace_marker.exists())

    def test_init_prints_post_setup_guide_with_custom_dir_commands(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            args = argparse.Namespace(config_dir=tmpdir, force=False, schema=False)
            resolved_dir = str(Path(tmpdir).resolve())

            with patch("xagent.interfaces.cli.collect_init_selection", return_value=_selection()):
                with patch("sys.stdout") as stdout:
                    exit_code = handle_init(args)

        self.assertEqual(exit_code, 0)
        output = "".join(call.args[0] for call in stdout.write.call_args_list if call.args)
        self.assertIn("Pick how you want to use it next", output)
        self.assertIn(f"xagent chat --dir {resolved_dir}", output)
        self.assertIn(f"xagent web --dir {resolved_dir}", output)
        self.assertIn(f"xagent service start api --dir {resolved_dir}", output)
        self.assertIn(f"xagent init feishu --dir {resolved_dir}", output)
        self.assertIn(f"xagent service start feishu --dir {resolved_dir}", output)
        self.assertIn(f"xagent doctor --dir {resolved_dir}", output)
        self.assertNotIn(f"xagent voice --dir {resolved_dir}", output)

    def test_init_prints_voice_entry_when_voice_is_configured(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            args = argparse.Namespace(config_dir=tmpdir, force=False, schema=False)
            resolved_dir = str(Path(tmpdir).resolve())

            with patch(
                "xagent.interfaces.cli.collect_init_selection",
                return_value=_selection(
                    voice_enabled=True,
                    voice_provider="qwen",
                    voice_api_key="voice-key",
                ),
            ):
                with patch("sys.stdout") as stdout:
                    exit_code = handle_init(args)

        self.assertEqual(exit_code, 0)
        output = "".join(call.args[0] for call in stdout.write.call_args_list if call.args)
        self.assertIn(f"xagent voice --dir {resolved_dir}", output)

    def test_init_feishu_updates_unified_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir)
            args = argparse.Namespace(
                config_dir=tmpdir,
                app_id=None,
                app_secret=None,
                manual=True,
                force=False,
            )

            with patch("builtins.input", return_value="cli_test") as input_mock:
                with patch("xagent.interfaces.cli.getpass.getpass", return_value="secret") as getpass_mock:
                    with patch("sys.stdout") as stdout:
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
        output = "".join(call.args[0] for call in stdout.write.call_args_list if call.args)
        self.assertIn("xagent service start feishu", output)

    def test_init_feishu_one_click_writes_registered_credentials(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir)
            args = argparse.Namespace(
                config_dir=tmpdir,
                app_id=None,
                app_secret=None,
                manual=False,
                force=False,
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
        self.assertIn("The browser authorization link will appear here next.", output)
        self.assertIn("Optional, only if you want to use the agent in group chats", output)
        self.assertIn("If you only use the agent in direct chat, you can skip this for now.", output)

    def test_init_feishu_one_click_cancelled_leaves_config_untouched(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir)
            args = argparse.Namespace(
                config_dir=tmpdir,
                app_id=None,
                app_secret=None,
                manual=False,
                force=False,
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
        self.assertIn("Authorization link ready.", output)
        self.assertIn("Verification code: Z9YC-ZV4A", output)
        self.assertIn("Link expires in: 60 minutes", output)
        self.assertIn("Browser link:\nhttps://open.feishu.cn/page/launcher?user_code=Z9YC-ZV4A&from=sdk&tp=sdk", output)
        self.assertNotIn("{'url':", output)

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

    def test_restart_defaults_to_all_enabled_channels(self):
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
        self.assertEqual(stopper.call_count, 2)
        self.assertEqual(starter.call_count, 2)

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
