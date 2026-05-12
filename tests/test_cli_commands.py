import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from xagent.interfaces.cli import (
    InitSelection,
    build_parser,
    handle_feishu_init,
    handle_feishu_start,
    handle_feishu_status,
    handle_feishu_stop,
    handle_init,
    handle_server,
    main,
)


def _selection() -> InitSelection:
    return InitSelection(
        provider="openai",
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="gpt-5.4-mini",
        identity="# Identity\n\nTest agent.\n",
        search_provider="openai",
    )


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

    def test_parser_supports_server_command(self):
        args = build_parser().parse_args([
            "server",
            "--dir",
            "./agent-dir",
            "--host",
            "127.0.0.1",
            "--port",
            "8010",
            "--no-web",
            "--max-concurrent-chats",
            "2",
            "--queue-timeout",
            "3.5",
            "--chat-timeout",
            "9.5",
        ])

        self.assertEqual(args.command, "server")
        self.assertEqual(args.config_dir, "./agent-dir")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8010)
        self.assertTrue(args.no_web)
        self.assertEqual(args.max_concurrent_chats, 2)
        self.assertEqual(args.queue_timeout, 3.5)
        self.assertEqual(args.chat_timeout, 9.5)

    def test_main_without_subcommand_prints_help(self):
        with patch("sys.stdout") as stdout:
            exit_code = main([])

        self.assertEqual(exit_code, 0)
        output = "".join(call.args[0] for call in stdout.write.call_args_list if call.args)
        self.assertIn("init", output)
        self.assertIn("chat", output)
        self.assertIn("server", output)

    def test_root_flags_are_not_supported(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["--init"])
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["--ask", "Hello"])

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

    def test_server_handler_passes_options_to_server(self):
        args = argparse.Namespace(
            config_dir="./agent-dir",
            no_web=True,
            max_concurrent_chats=2,
            queue_timeout=3.5,
            chat_timeout=9.5,
            host="127.0.0.1",
            port=8010,
            open_browser=True,
        )
        server_instance = MagicMock()

        with patch("xagent.interfaces.server.AgentHTTPServer", return_value=server_instance) as server_class:
            exit_code = handle_server(args)

        self.assertEqual(exit_code, 0)
        server_class.assert_called_once_with(
            config_dir="./agent-dir",
            enable_web=False,
            max_concurrent_chats=2,
            chat_queue_timeout=3.5,
            chat_timeout=9.5,
        )
        server_instance.run.assert_called_once_with(
            host="127.0.0.1",
            port=8010,
            open_browser=True,
        )

    def test_parser_supports_feishu_start_command(self):
        args = build_parser().parse_args([
            "feishu",
            "start",
            "--dir",
            "./agent-dir",
            "--config",
            "./agent-dir/feishu/feishu.yaml",
            "--foreground",
        ])

        self.assertEqual(args.command, "feishu")
        self.assertEqual(args.feishu_command, "start")
        self.assertEqual(args.config_dir, "./agent-dir")
        self.assertEqual(args.feishu_config, "./agent-dir/feishu/feishu.yaml")
        self.assertTrue(args.foreground)
        self.assertFalse(hasattr(args, "verbose"))

    def test_parser_rejects_old_feishu_run_command(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["feishu", "run"])

    def test_parser_supports_feishu_stop_command(self):
        args = build_parser().parse_args([
            "feishu",
            "stop",
            "--dir",
            "./agent-dir",
        ])

        self.assertEqual(args.command, "feishu")
        self.assertEqual(args.feishu_command, "stop")
        self.assertEqual(args.config_dir, "./agent-dir")

    def test_parser_supports_feishu_status_command(self):
        args = build_parser().parse_args([
            "feishu",
            "status",
            "--dir",
            "./agent-dir",
        ])

        self.assertEqual(args.command, "feishu")
        self.assertEqual(args.feishu_command, "status")
        self.assertEqual(args.config_dir, "./agent-dir")

    def test_feishu_init_prints_guidance_in_user_flow_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            args = argparse.Namespace(
                config_dir=tmpdir,
                feishu_config=None,
                app_id=None,
                app_secret=None,
                force=False,
            )

            with patch("builtins.input", return_value="cli_test") as input_mock:
                with patch("xagent.interfaces.cli.getpass.getpass", return_value="secret") as getpass_mock:
                    with patch("sys.stdout") as stdout:
                        exit_code = handle_feishu_init(args)
            feishu_config_exists = (Path(tmpdir) / "feishu" / "feishu.yaml").is_file()

        self.assertEqual(exit_code, 0)
        input_mock.assert_called_once_with("Feishu App ID: ")
        getpass_mock.assert_called_once_with("Feishu App Secret: ")
        self.assertTrue(feishu_config_exists)

        output = "".join(call.args[0] for call in stdout.write.call_args_list if call.args)
        launcher_index = output.index("https://open.feishu.cn/page/launcher")
        copy_index = output.index("Copy your App ID and App Secret.")
        wrote_index = output.index("Wrote ")
        finish_index = output.index("Finish setup in the Feishu Developer Console")
        app_index = output.index("https://open.feishu.cn/app")
        group_permission_index = output.index("im:message.group_msg")
        include_bot_permission_index = output.index("im:message.group_at_msg.include_bot:readonly")
        user_permission_index = output.index("contact:user.base:readonly")
        app_permission_index = output.index("admin:app.info:readonly")

        self.assertLess(launcher_index, copy_index)
        self.assertLess(copy_index, wrote_index)
        self.assertLess(wrote_index, finish_index)
        self.assertLess(finish_index, app_index)
        self.assertLess(app_index, group_permission_index)
        self.assertLess(group_permission_index, include_bot_permission_index)
        self.assertLess(include_bot_permission_index, user_permission_index)
        self.assertLess(user_permission_index, app_permission_index)
        self.assertIn("Run: `xagent feishu start` to start your bot!", output)

    def test_feishu_start_uses_background_by_default(self):
        args = argparse.Namespace(
            config_dir="./agent-dir",
            feishu_config="./agent-dir/feishu/feishu.yaml",
            foreground=False,
            feishu_foreground_internal=False,
        )

        with patch("xagent.interfaces.cli._start_feishu_background", return_value=0) as background_run:
            exit_code = handle_feishu_start(args)

        self.assertEqual(exit_code, 0)
        background_run.assert_called_once_with(args)

    def test_feishu_start_foreground_stays_attached(self):
        args = argparse.Namespace(
            config_dir="./agent-dir",
            feishu_config="./agent-dir/feishu/feishu.yaml",
            foreground=True,
            feishu_foreground_internal=False,
        )

        with patch("xagent.interfaces.cli._run_feishu_foreground", return_value=0) as foreground_run:
            exit_code = handle_feishu_start(args)

        self.assertEqual(exit_code, 0)
        foreground_run.assert_called_once_with(args)

    def test_feishu_stop_stops_running_process(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "feishu" / "feishu.pid"
            pid_path.parent.mkdir()
            pid_path.write_text("4321\n", encoding="utf-8")
            running = {"alive": True}

            def fake_kill(pid: int, sig: int) -> None:
                self.assertEqual(pid, 4321)
                if sig == 0:
                    if not running["alive"]:
                        raise ProcessLookupError()
                    return
                running["alive"] = False

            args = argparse.Namespace(config_dir=tmpdir)

            with patch("xagent.interfaces.cli.os.kill", side_effect=fake_kill):
                with patch("xagent.interfaces.cli.time.sleep"):
                    with patch("sys.stdout") as stdout:
                        exit_code = handle_feishu_stop(args)

        self.assertEqual(exit_code, 0)
        self.assertFalse(pid_path.exists())
        output = "".join(call.args[0] for call in stdout.write.call_args_list if call.args)
        self.assertIn("Stopped Feishu process 4321.", output)

    def test_feishu_status_reports_running_process(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            feishu_dir = Path(tmpdir) / "feishu"
            feishu_dir.mkdir()
            (feishu_dir / "feishu.pid").write_text("4321\n", encoding="utf-8")
            (feishu_dir / "feishu.yaml").write_text("app_id: cli_test\napp_secret: secret\n", encoding="utf-8")
            args = argparse.Namespace(config_dir=tmpdir)

            with patch("xagent.interfaces.cli.os.kill", return_value=None):
                with patch("sys.stdout") as stdout:
                    exit_code = handle_feishu_status(args)

        self.assertEqual(exit_code, 0)
        output = "".join(call.args[0] for call in stdout.write.call_args_list if call.args)
        self.assertIn(f"Feishu dir: {feishu_dir.resolve()}", output)
        self.assertIn("Config:", output)
        self.assertIn("Status: running (pid=4321)", output)
        self.assertIn("Stop: xagent feishu stop", output)


if __name__ == "__main__":
    unittest.main()
