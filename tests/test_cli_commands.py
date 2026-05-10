import argparse
import unittest
from unittest.mock import MagicMock, patch

from xagent.interfaces.cli import build_parser, handle_server, main


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

    def test_legacy_flags_are_not_supported(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["--init"])
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["--ask", "Hello"])

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


if __name__ == "__main__":
    unittest.main()
