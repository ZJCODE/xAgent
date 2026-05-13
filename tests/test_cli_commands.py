import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from xagent.interfaces.cli import (
    InitSelection,
    build_parser,
    handle_init,
    handle_init_feishu,
    handle_legacy_feishu,
    handle_legacy_server,
    handle_run_channel_internal,
    handle_start,
    handle_status,
    handle_stop,
    main,
)
from xagent.interfaces.processes import StartResult


def _selection() -> InitSelection:
    return InitSelection(
        provider="openai",
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="gpt-5.4-mini",
        identity="# Identity\n\nTest agent.\n",
        search_provider="openai",
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
            "http": {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 8010,
                "web": True,
            }
        },
        "runtime": {"default_channel": "web"},
    }
    if feishu:
        config["channels"]["feishu"] = {
            "enabled": True,
            "app_id": "cli_test",
            "app_secret": "secret",
        }
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (root / "identity.md").write_text("# Identity\n\nTest agent.\n", encoding="utf-8")


class CLICommandTests(unittest.TestCase):
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
            "--private",
        ])

        self.assertEqual(args.command, "chat")
        self.assertEqual(args.message, "Hello")
        self.assertEqual(args.config_dir, "./agent-dir")
        self.assertEqual(args.user_id, "alice")
        self.assertFalse(args.memory)
        self.assertTrue(args.private)

    def test_parser_supports_channel_lifecycle_commands(self):
        args = build_parser().parse_args([
            "start",
            "--dir",
            "./agent-dir",
            "--channel",
            "web,feishu",
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

        self.assertEqual(args.command, "start")
        self.assertEqual(args.config_dir, "./agent-dir")
        self.assertEqual(args.channels, ["web,feishu"])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8010)
        self.assertEqual(args.max_concurrent_chats, 2)
        self.assertEqual(args.queue_timeout, 3.5)
        self.assertEqual(args.chat_timeout, 9.5)

    def test_parser_supports_observe_and_management_commands(self):
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

        config = build_parser().parse_args(["config", "validate", "--dir", "./agent-dir"])
        self.assertEqual(config.config_command, "validate")

        memory = build_parser().parse_args(["memory", "search", "project", "--scope", "daily"])
        self.assertEqual(memory.memory_command, "search")
        self.assertEqual(memory.query, "project")

        messages = build_parser().parse_args(["messages", "list", "--count", "5"])
        self.assertEqual(messages.messages_command, "list")
        self.assertEqual(messages.count, 5)

    def test_main_without_subcommand_prints_help(self):
        with patch("sys.stdout") as stdout:
            exit_code = main([])

        self.assertEqual(exit_code, 0)
        output = "".join(call.args[0] for call in stdout.write.call_args_list if call.args)
        self.assertIn("init", output)
        self.assertIn("chat", output)
        self.assertIn("start", output)
        self.assertIn("status", output)

    def test_init_force_can_keep_runtime_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "config.yaml").write_text("provider:\n  model: old\n", encoding="utf-8")
            (root / "identity.md").write_text("old", encoding="utf-8")
            memory_marker = root / "memory" / "entry.md"
            messages_marker = root / "messages" / "messages.sqlite3"
            memory_marker.parent.mkdir()
            messages_marker.parent.mkdir()
            memory_marker.write_text("keep-memory", encoding="utf-8")
            messages_marker.write_text("keep-messages", encoding="utf-8")
            args = argparse.Namespace(config_dir=tmpdir, force=True, schema=False)

            with patch("xagent.interfaces.cli._prompt_yes_no", return_value=False) as prompt:
                with patch("xagent.interfaces.cli.collect_init_selection", return_value=_selection()):
                    exit_code = handle_init(args)

            self.assertEqual(exit_code, 0)
            prompt.assert_called_once()
            self.assertEqual(memory_marker.read_text(encoding="utf-8"), "keep-memory")
            self.assertEqual(messages_marker.read_text(encoding="utf-8"), "keep-messages")

    def test_init_force_can_clear_runtime_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "config.yaml").write_text("provider:\n  model: old\n", encoding="utf-8")
            (root / "identity.md").write_text("old", encoding="utf-8")
            memory_marker = root / "memory" / "entry.md"
            messages_marker = root / "messages" / "messages.sqlite3"
            memory_marker.parent.mkdir()
            messages_marker.parent.mkdir()
            memory_marker.write_text("clear-memory", encoding="utf-8")
            messages_marker.write_text("clear-messages", encoding="utf-8")
            args = argparse.Namespace(config_dir=tmpdir, force=True, schema=False)

            with patch("xagent.interfaces.cli._prompt_yes_no", return_value=True) as prompt:
                with patch("xagent.interfaces.cli.collect_init_selection", return_value=_selection()):
                    exit_code = handle_init(args)

            self.assertEqual(exit_code, 0)
            prompt.assert_called_once()
            self.assertTrue((root / "memory").is_dir())
            self.assertTrue((root / "messages").is_dir())
            self.assertFalse(memory_marker.exists())
            self.assertFalse(messages_marker.exists())

    def test_init_feishu_updates_unified_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir)
            args = argparse.Namespace(
                config_dir=tmpdir,
                app_id=None,
                app_secret=None,
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
        self.assertTrue(config["channels"]["feishu"]["enabled"])
        self.assertTrue(config["channels"]["feishu"]["show_sender_ids"])
        output = "".join(call.args[0] for call in stdout.write.call_args_list if call.args)
        self.assertIn("xagent start --channel feishu", output)

    def test_run_channel_web_passes_options_to_server(self):
        args = argparse.Namespace(
            channel="web",
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
            enable_web=True,
            max_concurrent_chats=2,
            chat_queue_timeout=3.5,
            chat_timeout=9.5,
        )
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
                channels=["web,feishu"],
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

    def test_stop_uses_managed_pid_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir, feishu=True)
            args = argparse.Namespace(config_dir=tmpdir, channels=["feishu"])

            with patch("xagent.interfaces.cli.stop_managed_process", return_value=(True, "stopped")) as stopper:
                exit_code = handle_stop(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(stopper.call_args.args[0], Path(tmpdir).resolve() / "run" / "feishu.pid")

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

    def test_http_and_web_cannot_be_selected_together(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write_runtime(tmpdir)
            args = argparse.Namespace(
                config_dir=tmpdir,
                channels=["http,web"],
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
        self.assertIn("web already includes", output)

    def test_legacy_commands_print_migration_guidance(self):
        with patch("sys.stdout") as stdout:
            server_code = handle_legacy_server(argparse.Namespace())
            feishu_code = handle_legacy_feishu(argparse.Namespace())

        self.assertEqual(server_code, 1)
        self.assertEqual(feishu_code, 1)
        output = "".join(call.args[0] for call in stdout.write.call_args_list if call.args)
        self.assertIn("xagent run --channel web", output)
        self.assertIn("xagent init feishu", output)


if __name__ == "__main__":
    unittest.main()
