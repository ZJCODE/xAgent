from __future__ import annotations

import contextlib
import io
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from pydantic import ValidationError

from xagent.interfaces.cli import build_parser, main
from xagent.interfaces.cli.web import web_url
from xagent.settings import XAgentSettings


def _settings_data() -> dict:
    return {
        "schema_version": 2,
        "provider": {
            "name": "openai",
            "model": "test-model",
            "api_key": "test",
        },
        "tools": {"shell": {"enabled": False}},
        "channels": {
            "api": {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 8010,
            },
        },
    }


class SettingsSchemaTests(unittest.TestCase):
    def test_schema_version_two_is_mandatory(self):
        data = _settings_data()
        data.pop("schema_version")
        with self.assertRaises(ValidationError):
            XAgentSettings.model_validate(data)

    def test_shell_is_built_in_by_default_and_unknown_top_level_keys_fail(self):
        data = _settings_data()
        data.pop("tools")
        settings = XAgentSettings.model_validate(data)
        self.assertTrue(settings.tools.shell.enabled)
        data["unknown_runtime"] = {}
        with self.assertRaises(ValidationError):
            XAgentSettings.model_validate(data)

    def test_atomic_round_trip_uses_the_single_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            settings = XAgentSettings.model_validate(_settings_data())
            settings.write_atomic(path)
            loaded = XAgentSettings.load(path)
            self.assertEqual(loaded, settings)
            self.assertFalse(path.with_suffix(".yaml.tmp").exists())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


class CleanCliParserTests(unittest.TestCase):
    def help_text(self, *arguments: str) -> str:
        parser = build_parser()
        if not arguments:
            return parser.format_help()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                parser.parse_args([*arguments, "-h"])
        self.assertEqual(raised.exception.code, 0)
        return output.getvalue()

    def test_runtime_and_channel_commands_are_the_public_surface(self):
        parser = build_parser()
        self.assertEqual(parser.parse_args(["run"]).command, "run")
        self.assertEqual(parser.parse_args(["web"]).command, "web")
        channel = parser.parse_args(["channel", "restart", "feishu"])
        self.assertEqual(channel.channel_action, "restart")
        self.assertEqual(channel.name, "feishu")
        delivery = parser.parse_args(
            ["delivery", "list", "--status", "blocked", "--json"]
        )
        self.assertEqual(delivery.status, "blocked")
        self.assertTrue(delivery.json_output)

    def test_old_per_channel_commands_are_not_accepted(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["api", "start"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["_runtime"])

    def test_launcher_is_a_first_class_command(self):
        parser = build_parser()
        self.assertEqual(parser.parse_args(["launcher"]).command, "launcher")

    def test_no_argument_tty_opens_the_launcher(self):
        with patch(
            "xagent.interfaces.cli.rich_terminal_available",
            return_value=True,
        ), patch(
            "xagent.interfaces.cli.launcher.run_launcher",
            return_value=0,
        ) as launcher:
            self.assertEqual(main([]), 0)
        launcher.assert_called_once()

    def test_top_level_help_documents_the_complete_current_surface(self):
        text = self.help_text()
        for expected in (
            "xagent <command> [options]",
            "setup",
            "launcher",
            "web",
            "run",
            "start",
            "stop",
            "restart",
            "status",
            "chat [message]",
            "channel setup <name>",
            "delivery retry <id>",
            "person link ...",
            "The Runtime remains alive when every channel is disabled.",
            "Deliveries blocked by a disabled channel are never sent until retried.",
            "xagent <command> -h",
            "desktop browser management UI",
            "headless terminal control surface",
        ):
            self.assertIn(expected, text)
        self.assertNotIn("_runtime", text)
        self.assertNotIn("api start", text)
        self.assertNotIn("migration", text.lower())

    def test_every_public_command_has_description_options_and_examples(self):
        commands = (
            ("setup",),
            ("launcher",),
            ("web",),
            ("run",),
            ("start",),
            ("stop",),
            ("restart",),
            ("status",),
            ("chat",),
            ("channel",),
            ("channel", "list"),
            ("channel", "setup"),
            ("channel", "setup", "api"),
            ("channel", "setup", "feishu"),
            ("channel", "setup", "weixin"),
            ("channel", "setup", "voice"),
            ("channel", "start"),
            ("channel", "stop"),
            ("channel", "restart"),
            ("delivery",),
            ("delivery", "list"),
            ("delivery", "retry"),
            ("person",),
            ("person", "list"),
            ("person", "link"),
        )
        for command in commands:
            with self.subTest(command=command):
                text = self.help_text(*command)
                self.assertIn("usage:", text)
                self.assertIn("Examples:", text)
                self.assertIn("-h, --help", text)

    def test_web_command_is_loopback_only_and_has_explicit_browser_control(self):
        parser = build_parser()
        parsed = parser.parse_args(
            [
                "web",
                "--host",
                "::1",
                "--port",
                "8080",
                "--no-open",
                "--agent",
                "mono",
            ]
        )
        self.assertEqual(parsed.host, "::1")
        self.assertEqual(parsed.port, 8080)
        self.assertFalse(parsed.open_browser)
        self.assertEqual(parsed.agent, "mono")
        self.assertEqual(web_url(parsed.host, parsed.port), "http://[::1]:8080")

        with self.assertRaises(SystemExit):
            parser.parse_args(["web", "--host", "0.0.0.0"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["web", "--port", "0"])

    def test_channel_setup_help_exposes_only_relevant_options(self):
        api = self.help_text("channel", "setup", "api")
        feishu = self.help_text("channel", "setup", "feishu")
        weixin = self.help_text("channel", "setup", "weixin")
        voice = self.help_text("channel", "setup", "voice")

        self.assertIn("--host", api)
        self.assertIn("--port", api)
        self.assertNotIn("--app-id", api)
        self.assertNotIn("--provider", api)

        self.assertIn("--app-id", feishu)
        self.assertIn("--group-fetch-limit", feishu)
        self.assertNotIn("--base-url", feishu)
        self.assertNotIn("--stt-provider", feishu)

        self.assertIn("--base-url", weixin)
        self.assertIn("--allow-user", weixin)
        self.assertNotIn("--app-id", weixin)
        self.assertNotIn("--stt-provider", weixin)

        self.assertIn("--provider", voice)
        self.assertIn("--stt-provider", voice)
        self.assertIn("--wake-phrase", voice)
        self.assertNotIn("--app-id", voice)
        self.assertNotIn("--base-url", voice)

    def test_channel_specific_parsers_keep_the_handler_contract(self):
        parser = build_parser()
        feishu = parser.parse_args(
            [
                "channel",
                "setup",
                "feishu",
                "--manual",
                "--app-id",
                "app",
                "--app-secret",
                "secret",
                "--force",
            ]
        )
        self.assertEqual((feishu.channel_action, feishu.name), ("setup", "feishu"))
        self.assertTrue(feishu.manual)
        self.assertTrue(feishu.force)

        weixin = parser.parse_args(
            ["channel", "setup", "weixin", "--allow-user", "alice", "--no-media"]
        )
        self.assertEqual(weixin.allow_users, ["alice"])
        self.assertFalse(weixin.media_enabled)

        voice = parser.parse_args(
            ["channel", "setup", "voice", "--provider", "custom", "--wake"]
        )
        self.assertEqual(voice.provider, "custom")
        self.assertTrue(voice.wake)

        with self.assertRaises(SystemExit):
            parser.parse_args(["channel", "setup", "api", "--app-id", "invalid"])
