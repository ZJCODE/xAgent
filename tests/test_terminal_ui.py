import io
import unittest
from unittest.mock import patch

import readchar
from rich.console import Console

from xagent.interfaces.cli.terminal_ui import MenuOption, ReturnToLauncherHome, TerminalUI


def _interactive_ui() -> tuple[TerminalUI, io.StringIO]:
    stream = io.StringIO()
    ui = TerminalUI(stdout=stream)
    ui.interactive = True
    ui.console = Console(file=stream, force_terminal=True, highlight=False, width=80)
    return ui, stream


class TerminalUITests(unittest.TestCase):
    def test_print_panel_is_borderless(self):
        stdout = io.StringIO()
        ui = TerminalUI(stdout=stdout)

        ui.print_panel("Ready.", title="Status")

        output = stdout.getvalue()
        self.assertIn("Status", output)
        self.assertIn("Ready.", output)
        self.assertNotIn("╭", output)
        self.assertNotIn("│", output)
        self.assertNotIn("╰", output)

    def test_render_menu_text_is_borderless(self):
        ui = TerminalUI(stdout=io.StringIO())

        rendered = str(
            ui._render_menu_text(
                title="xAgent",
                subtitle="Runtime ready",
                options=[
                    MenuOption("chat", "Chat", "Talk with the configured agent."),
                    MenuOption("web", "Web", "Manage the browser client."),
                ],
                footer="↑/↓ move  •  enter select  •  esc quit",
                selected=0,
            )
        )

        self.assertIn("xAgent", rendered)
        self.assertIn("› Chat", rendered)
        self.assertIn("Web", rendered)
        self.assertNotIn("╭", rendered)
        self.assertNotIn("│", rendered)
        self.assertNotIn("╰", rendered)

    def test_select_records_the_resolved_choice(self):
        ui, stream = _interactive_ui()
        keys = iter([readchar.key.DOWN, readchar.key.ENTER])

        with patch("xagent.interfaces.cli.terminal_ui.readchar.readkey", side_effect=lambda: next(keys)):
            choice = ui.select(
                label="Provider",
                options=[
                    MenuOption("openai", "openai", "GPT family."),
                    MenuOption("deepseek", "deepseek", "DeepSeek models."),
                ],
            )

        self.assertIsNotNone(choice)
        self.assertEqual(choice.key, "deepseek")
        output = stream.getvalue()
        self.assertIn("✔", output)
        self.assertIn("Provider", output)

    def test_confirm_uses_arrow_menu(self):
        ui, _stream = _interactive_ui()
        keys = iter([readchar.key.ENTER])

        with patch("xagent.interfaces.cli.terminal_ui.readchar.readkey", side_effect=lambda: next(keys)):
            result = ui.confirm("Enable voice mode?", default=True)

        self.assertTrue(result)

    def test_confirm_cancel_returns_none(self):
        ui, _stream = _interactive_ui()
        keys = iter(["\x1b"])

        with patch("xagent.interfaces.cli.terminal_ui.readchar.readkey", side_effect=lambda: next(keys)):
            result = ui.confirm("Write project files?", default=True)

        self.assertIsNone(result)

    def test_select_menu_h_shortcut_returns_to_launcher_home(self):
        ui, _stream = _interactive_ui()
        keys = iter(["h"])

        with patch("xagent.interfaces.cli.terminal_ui.readchar.readkey", side_effect=lambda: next(keys)):
            with self.assertRaises(ReturnToLauncherHome):
                ui.select_menu(
                    title="xAgent Setup",
                    subtitle="Runtime ready",
                    options=[
                        MenuOption("partial", "Edit Setup", "Update one feature."),
                        MenuOption("back", "Back", "Return to launcher."),
                    ],
                    footer="↑/↓ Move • Enter Select  •  q Back",
                )

    def test_ask_text_masks_secret_and_records(self):
        ui, stream = _interactive_ui()
        keys = iter(["s", "e", "c", readchar.key.ENTER])

        with patch("xagent.interfaces.cli.terminal_ui.readchar.readkey", side_effect=lambda: next(keys)):
            value = ui.ask_secret("API key (leave blank to fill in later): ")

        self.assertEqual(value, "sec")
        output = stream.getvalue()
        self.assertIn("••••••", output)
        self.assertNotIn("sec", output)

    def test_ask_text_applies_default_when_blank(self):
        ui, _stream = _interactive_ui()
        keys = iter([readchar.key.ENTER])

        with patch("xagent.interfaces.cli.terminal_ui.readchar.readkey", side_effect=lambda: next(keys)):
            value = ui.ask_text("Base URL", default="https://example.com")

        self.assertEqual(value, "https://example.com")


if __name__ == "__main__":
    unittest.main()