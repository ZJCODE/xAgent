"""Rich/readchar terminal helpers for the xAgent CLI."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Optional, Sequence

import readchar  # type: ignore[import-not-found]
from rich.console import Console  # type: ignore[import-not-found]
from rich.live import Live  # type: ignore[import-not-found]
from rich.text import Text  # type: ignore[import-not-found]


@dataclass(frozen=True)
class MenuOption:
    """Single launcher or wizard row rendered in the terminal menu."""

    key: str
    title: str
    description: str
    disabled: bool = False


class ReturnToLauncherHome(Exception):
    """Raised when a launcher menu requests a jump back to the main home screen."""


def rich_terminal_available(*, stdin=None, stdout=None) -> bool:
    """Return whether the process can use the Rich/readchar interactive UI."""
    stdin_stream = stdin or sys.stdin
    stdout_stream = stdout or sys.stdout
    stdin_is_tty = getattr(stdin_stream, "isatty", lambda: False)()
    stdout_is_tty = getattr(stdout_stream, "isatty", lambda: False)()
    return bool(stdin_is_tty and stdout_is_tty)


class TerminalUI:
    """Shared helpers for launcher-style terminal menus."""

    def __init__(self, *, stdin=None, stdout=None):
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.interactive = rich_terminal_available(stdin=self.stdin, stdout=self.stdout)
        self.console = Console(file=self.stdout, highlight=False)

    def clear(self) -> None:
        if self.interactive:
            self.console.clear()

    def _title_style(self, border_style: str) -> str:
        if border_style == "red":
            return "bold red"
        if border_style == "green":
            return "bold green"
        if border_style == "yellow":
            return "bold yellow"
        return "bold"

    def print_panel(
        self,
        message,
        *,
        title: Optional[str] = None,
        border_style: str = "dim",
        leading_blank_line: bool = False,
    ) -> None:
        if leading_blank_line:
            self.console.print()
        text = Text()
        if title:
            text.append(f"{title}\n", style=self._title_style(border_style))
            if message:
                text.append("\n")
        if message:
            if isinstance(message, Text):
                text.append_text(message)
            else:
                text.append(str(message))
        self.console.print(text, highlight=False, soft_wrap=True)
        self.console.print()

    def pause(self, message: str = "Press Enter to continue") -> None:
        if not self.interactive:
            return
        try:
            self.console.input(f"[dim]{message}[/dim]")
        except EOFError:
            return

    def input(self, prompt: str) -> str:
        try:
            return self.console.input(prompt)
        except EOFError:
            return ""

    def _render_menu_text(
        self,
        *,
        title: str,
        options: Sequence[MenuOption],
        subtitle: str,
        footer: str,
        selected: int,
    ) -> Text:
        text = Text()
        text.append(f"{title}\n", style="bold")
        if subtitle:
            text.append(f"{subtitle}\n", style="grey50")
        text.append("\n")

        for index, option in enumerate(options):
            active = index == selected
            prefix = "› " if active else "  "
            if option.disabled:
                title_style = "bold bright_black" if active else "bright_black"
                desc_style = "bright_black"
            elif active:
                title_style = "cyan"
                desc_style = "grey50"
            else:
                title_style = "default"
                desc_style = "grey50"

            text.append(prefix + option.title + "\n", style=title_style)
            if option.description:
                text.append("    " + option.description + "\n", style=desc_style)

        text.append("\n")
        text.append(footer, style="grey50")
        return text

    def _print_choice(self, label: str, value: str, *, skipped: bool = False) -> None:
        text = Text()
        text.append("✔️  " if not skipped else "• ", style="green" if not skipped else "grey50")
        text.append(label, style="bold")
        text.append("  ")
        text.append(value, style="cyan" if not skipped else "grey50")
        self.console.print(text)

    def record(self, label: str, value: str, *, skipped: bool = False) -> None:
        """Print a persistent, collapsed summary line for a resolved step."""
        self._print_choice(label, value, skipped=skipped)

    def _run_menu(
        self,
        *,
        title: str,
        options: Sequence[MenuOption],
        subtitle: str,
        footer: str,
        default_index: int,
        screen: bool,
        home_shortcut: bool,
    ) -> Optional[MenuOption]:
        selected = max(0, min(default_index, len(options) - 1))
        up_keys = {readchar.key.UP, "k"}
        down_keys = {readchar.key.DOWN, "j"}
        enter_keys = {readchar.key.ENTER, "\r", "\n"}
        quit_keys = {"q", "Q", "\x03", "\x1b"}
        home_keys = {"h", "H"}

        def render() -> Text:
            return self._render_menu_text(
                title=title,
                options=options,
                subtitle=subtitle,
                footer=footer,
                selected=selected,
            )

        if not screen:
            self.console.print()

        with Live(render(), console=self.console, screen=screen, auto_refresh=False, transient=True) as live:
            live.update(render(), refresh=True)
            while True:
                key = readchar.readkey()
                if key in up_keys:
                    selected = (selected - 1) % len(options)
                    live.update(render(), refresh=True)
                    continue
                if key in down_keys:
                    selected = (selected + 1) % len(options)
                    live.update(render(), refresh=True)
                    continue
                if key in enter_keys:
                    option = options[selected]
                    if option.disabled:
                        continue
                    return option
                if home_shortcut and key in home_keys:
                    raise ReturnToLauncherHome()
                if key in quit_keys:
                    return None

    def _read_line(self, label: str, *, default: Optional[str], secret: bool, subtitle: str = "") -> str:
        buffer: list[str] = []

        def render() -> Text:
            text = Text()
            if subtitle:
                text.append(subtitle, style="grey50")
                text.append("\n")
            text.append("?  ", style="cyan")
            text.append(label, style="bold")
            if default:
                text.append(f"  [{default}]", style="grey50")
            text.append("\n› ", style="grey50")
            if buffer:
                text.append("•" * len(buffer) if secret else "".join(buffer))
            return text

        self.console.print()
        with Live(render(), console=self.console, transient=True, auto_refresh=False) as live:
            live.update(render(), refresh=True)
            while True:
                key = readchar.readkey()
                if key in (readchar.key.ENTER, "\r", "\n"):
                    break
                if key in (readchar.key.BACKSPACE, "\x7f", "\x08"):
                    if buffer:
                        buffer.pop()
                        live.update(render(), refresh=True)
                    continue
                if key == "\x03":
                    raise KeyboardInterrupt()
                if len(key) == 1 and key.isprintable():
                    buffer.append(key)
                    live.update(render(), refresh=True)
        return "".join(buffer)

    def _fallback_select_menu(
        self,
        *,
        title: str,
        options: Sequence[MenuOption],
        subtitle: str,
        default_index: int,
        home_shortcut: bool,
    ) -> Optional[MenuOption]:
        self.console.print(f"\n{title}")
        if subtitle:
            self.console.print(subtitle, style="dim")
        self.console.print()

        visible_options = [option for option in options if option.key != "back"]
        default_choice = max(0, min(default_index, len(visible_options) - 1)) if visible_options else 0
        for index, option in enumerate(visible_options, 1):
            suffix = " [disabled]" if option.disabled else ""
            self.console.print(f"  {index}. {option.title}{suffix}")
            if option.description:
                self.console.print(f"     {option.description}", style="dim")

        while True:
            prompt = "Choose an option number"
            if home_shortcut:
                prompt += " (or h for home, q to go back): "
            else:
                prompt += " (or q to go back): "
            raw_choice = self.input(prompt).strip()
            if not raw_choice:
                return visible_options[default_choice] if visible_options else None
            if home_shortcut and raw_choice.lower() in {"h", "home"}:
                raise ReturnToLauncherHome()
            if raw_choice.lower() in {"q", "quit", "exit"}:
                return None
            if raw_choice.isdigit():
                choice = int(raw_choice)
                if 1 <= choice <= len(visible_options):
                    selected = visible_options[choice - 1]
                    if selected.disabled:
                        self.print_panel("This option is not available yet.", title="Not Ready")
                        continue
                    return selected
            self.print_panel(f"Please enter a number from 1 to {len(visible_options)}.", title="Input Required")

    def select_menu(
        self,
        *,
        title: str,
        options: Sequence[MenuOption],
        subtitle: str = "",
        footer: str = "↑/↓ Move · Enter Select · q Back",
        default_index: int = 0,
        home_shortcut: bool = False,
    ) -> Optional[MenuOption]:
        """Navigation-style menu (stable full screen) used by launcher hubs."""
        if not options:
            return None

        home_shortcut = home_shortcut or "q Back" in footer
        rendered_footer = footer
        if home_shortcut and "h Home" not in rendered_footer:
            rendered_footer = f"{rendered_footer}  •  h Home"

        if not self.interactive:
            return self._fallback_select_menu(
                title=title,
                options=options,
                subtitle=subtitle,
                default_index=max(0, min(default_index, len(options) - 1)),
                home_shortcut=home_shortcut,
            )

        return self._run_menu(
            title=title,
            options=options,
            subtitle=subtitle,
            footer=rendered_footer,
            default_index=default_index,
            screen=True,
            home_shortcut=home_shortcut,
        )

    def select(
        self,
        *,
        label: str,
        options: Sequence[MenuOption],
        subtitle: str = "",
        default_index: int = 0,
    ) -> Optional[MenuOption]:
        """Form-style single select that renders inline and records the choice."""
        if not options:
            return None

        default_index = max(0, min(default_index, len(options) - 1))
        if not self.interactive:
            choice = self._fallback_select_menu(
                title=label,
                options=options,
                subtitle=subtitle,
                default_index=default_index,
                home_shortcut=False,
            )
        else:
            choice = self._run_menu(
                title=label,
                options=options,
                subtitle=subtitle,
                footer="↑/↓ Move · Enter Select · q Cancel",
                default_index=default_index,
                screen=False,
                home_shortcut=False,
            )
        if choice is not None:
            self._print_choice(label, choice.title)
        return choice

    def confirm(self, label: str, *, default: bool = True) -> Optional[bool]:
        """Yes/No question rendered with the same arrow-key interaction as menus."""
        choice = self.select(
            label=label,
            options=[MenuOption("yes", "Yes", ""), MenuOption("no", "No", "")],
            default_index=0 if default else 1,
        )
        if choice is None:
            return None
        return choice.key == "yes"

    def ask_text(self, label: str, *, default: Optional[str] = None, secret: bool = False, subtitle: str = "") -> str:
        """Inline text/secret prompt that records a collapsed summary line."""
        if not self.interactive:
            suffix = f" [{default}]" if default else ""
            raw = self.input(f"{label}{suffix}: ")
            value = raw.strip()
            if not value and default is not None:
                value = default
            return value

        raw = self._read_line(label, default=default, secret=secret, subtitle=subtitle)
        value = raw.strip()
        if not value and default is not None:
            value = default
        if secret:
            display = "••••••" if value else "(skipped)"
        else:
            display = value or "(skipped)"
        self._print_choice(label, display, skipped=not value)
        return value

    def ask_secret(self, prompt: str) -> str:
        """Secret entry compatible with ``Callable[[str], str]`` injection points."""
        label = prompt.rstrip()
        if label.endswith(":"):
            label = label[:-1].rstrip()
        return self.ask_text(label, secret=True)