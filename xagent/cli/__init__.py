"""CLI facade."""

from importlib import import_module
from typing import Optional, Sequence

_main_module = import_module(".main", __name__)

globals().update({
    name: value
    for name, value in vars(_main_module).items()
    if not name.startswith("__") and name != "main"
})


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the CLI through the package facade."""

    original_rich_terminal_available = _main_module.rich_terminal_available
    original_run_interactive_launcher = _main_module._run_interactive_launcher
    _main_module.rich_terminal_available = globals()["rich_terminal_available"]
    _main_module._run_interactive_launcher = globals()["_run_interactive_launcher"]
    try:
        return _main_module.main(argv)
    finally:
        _main_module.rich_terminal_available = original_rich_terminal_available
        _main_module._run_interactive_launcher = original_run_interactive_launcher


__all__ = [
    name
    for name in globals()
    if not name.startswith("__") and name != "_main_module"
]
