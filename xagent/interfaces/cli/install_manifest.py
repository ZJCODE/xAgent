"""Records how GUI clients can launch the xAgent CLI.

GUI processes (the Electron desktop app) do not inherit the terminal's PATH,
virtualenv activation, or conda environment, so they cannot reliably locate the
``xagent`` executable on their own. Instead of guessing, the CLI writes a
launch command to ``~/.xagent/cli.json`` every time it runs. The desktop app
reads that single file to decide how to start the local web backend.
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..base import BaseAgentConfig

MANIFEST_FILENAME = "cli.json"


def manifest_path() -> Path:
    root = Path(BaseAgentConfig.DEFAULT_CONFIG_DIR).expanduser()
    return root / MANIFEST_FILENAME


def resolve_cli_binary() -> Path | None:
    """Return the absolute path to the ``xagent`` executable, if known.

    Prefers ``xagent`` on PATH. Falls back to ``sys.argv[0]`` only when it was
    invoked as the ``xagent`` console script (not ``python -m ...``).
    """
    found = shutil.which("xagent")
    if found:
        return Path(found).resolve()

    argv0 = sys.argv[0] if sys.argv else ""
    if argv0:
        candidate = Path(argv0)
        if candidate.stem == "xagent" and candidate.exists():
            return candidate.resolve()
    return None


def resolve_cli_command() -> list[str] | None:
    """Return the command that should re-enter this CLI later."""
    argv0 = Path(sys.argv[0]) if sys.argv else None
    if argv0 and argv0.name == "__main__.py" and argv0.parent.name == "xagent":
        return [sys.executable, "-m", "xagent"]

    binary = resolve_cli_binary()
    if binary is not None:
        return [str(binary)]
    return None


def _current_version() -> str | None:
    try:
        from ... import __version__

        return str(__version__)
    except Exception:
        return None


def record_cli_location() -> None:
    """Write the current CLI launch command to the manifest. Never raises."""
    try:
        command = resolve_cli_command()
        if command is None:
            return
        binary = resolve_cli_binary()
        path = manifest_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "command": command,
            "binary": str(binary) if binary is not None else "",
            "version": _current_version(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except Exception:
        # Recording is best-effort; a failure here must never break the CLI.
        pass


def read_cli_command() -> list[str] | None:
    """Return the recorded CLI command if the manifest contains one."""
    try:
        raw = manifest_path().read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    command = data.get("command")
    if not isinstance(command, list) or not command:
        return None
    if not all(isinstance(item, str) and item for item in command):
        return None
    return list(command)


def read_cli_binary() -> Path | None:
    """Return the diagnostic CLI binary path if it points at a real file."""
    try:
        raw = manifest_path().read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    binary = data.get("binary")
    if not isinstance(binary, str) or not binary:
        return None
    candidate = Path(binary)
    return candidate if candidate.exists() else None
