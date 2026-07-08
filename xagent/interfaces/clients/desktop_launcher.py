"""Electron desktop client launcher helpers."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Optional

from ..cli.clients import CLIENT_DESKTOP, web_client_config, web_client_public_url

PRODUCT_NAME = "xAgent"
DESKTOP_APP_ENV = "XAGENT_DESKTOP_APP"


class DesktopClientError(RuntimeError):
    """Raised when the desktop client cannot be launched."""


def desktop_app_dir() -> Path:
    """Return the development desktop app root containing package.json."""
    roots = (
        Path(__file__).resolve().parents[3],
        Path(__file__).resolve().parents[2],
        Path.cwd(),
    )
    seen: set[Path] = set()
    for root in roots:
        candidate = root / "desktop"
        if candidate in seen:
            continue
        seen.add(candidate)
        if (candidate / "package.json").is_file():
            return candidate.resolve()
    raise DesktopClientError(
        "Desktop app files not found. Install the desktop app from GitHub Releases, "
        "or clone the repository and run the build in desktop/."
    )


def desktop_setup_hint() -> str:
    packaged = packaged_desktop_app()
    if packaged is not None:
        return f"Installed app: {packaged}"
    return (
        "Install the desktop app from GitHub Releases, or build locally with: "
        "cd desktop && npm install && npm run build"
    )


def packaged_desktop_app() -> Optional[Path]:
    """Return a packaged desktop binary when installed on the system."""
    override = os.environ.get(DESKTOP_APP_ENV, "").strip()
    if override:
        path = Path(override).expanduser()
        if path.is_dir() and path.suffix == ".app":
            path = path / "Contents" / "MacOS" / PRODUCT_NAME
        if path.is_file():
            return path.resolve()

    for candidate in _packaged_desktop_candidates():
        if candidate.is_file():
            return candidate.resolve()
    return None


def desktop_dependencies_ready(desktop_dir: Optional[Path] = None) -> bool:
    if packaged_desktop_app() is not None:
        return True
    try:
        root = (desktop_dir or desktop_app_dir()).resolve()
    except DesktopClientError:
        return False
    return _electron_binary(root) is not None


def desktop_launch_env(config: Mapping[str, Any], *, api_url: Optional[str] = None) -> dict[str, str]:
    env = dict(os.environ)
    env["XAGENT_WEB_URL"] = web_client_public_url(_config_with_api_url(config, api_url))
    env["XAGENT_APP_TITLE"] = PRODUCT_NAME
    return env


def desktop_launch_command(
    config: Mapping[str, Any],
    *,
    api_url: Optional[str] = None,
    desktop_dir: Optional[Path] = None,
) -> list[str]:
    packaged = packaged_desktop_app()
    if packaged is not None:
        return [str(packaged)]

    root = (desktop_dir or desktop_app_dir()).resolve()
    electron_bin = _electron_binary(root)
    if electron_bin is None:
        raise DesktopClientError(
            f"Desktop client is not available. {desktop_setup_hint()}"
        )
    if electron_bin.name == "npx":
        return [str(electron_bin), "--prefix", str(root), "electron", str(root)]
    return [str(electron_bin), str(root)]


def launch_desktop_client(
    config: Mapping[str, Any],
    *,
    api_url: Optional[str] = None,
    desktop_dir: Optional[Path] = None,
    wait: bool = True,
) -> int:
    command = desktop_launch_command(config, api_url=api_url, desktop_dir=desktop_dir)
    env = desktop_launch_env(config, api_url=api_url)
    cwd = None if packaged_desktop_app() is not None else str(desktop_dir or desktop_app_dir())
    if wait:
        result = subprocess.run(command, cwd=cwd, env=env, check=False)
        return int(result.returncode)
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    return process.pid


def _config_with_api_url(config: Mapping[str, Any], api_url: Optional[str]) -> dict[str, Any]:
    if not api_url:
        return dict(config)
    clients = config.get("clients") if isinstance(config.get("clients"), Mapping) else {}
    clients = dict(clients) if isinstance(clients, Mapping) else {}
    web_cfg = dict(web_client_config(config))
    web_cfg["api_url"] = api_url.rstrip("/")
    clients["web"] = web_cfg
    merged = dict(config)
    merged["clients"] = clients
    return merged


def _packaged_desktop_candidates() -> list[Path]:
    if sys.platform == "darwin":
        return [
            Path("/Applications") / f"{PRODUCT_NAME}.app" / "Contents" / "MacOS" / PRODUCT_NAME,
            Path.home() / "Applications" / f"{PRODUCT_NAME}.app" / "Contents" / "MacOS" / PRODUCT_NAME,
        ]

    if os.name == "nt":
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        local_app = Path(os.environ.get("LOCALAPPDATA", ""))
        return [
            program_files / PRODUCT_NAME / f"{PRODUCT_NAME}.exe",
            local_app / "Programs" / PRODUCT_NAME / f"{PRODUCT_NAME}.exe",
        ]

    which_name = shutil.which("xagent-desktop")
    candidates = [
        Path(which_name) if which_name else Path(),
        Path.home() / ".local" / "bin" / "xagent-desktop",
        Path("/usr/local/bin/xagent-desktop"),
    ]
    candidates.extend(_appimage_candidates())
    return [path for path in candidates if str(path)]


def _appimage_candidates() -> list[Path]:
    search_roots = (
        Path.home() / "Applications",
        Path.home() / ".local" / "bin",
        Path.home() / "Downloads",
        Path.cwd(),
    )
    matches: list[Path] = []
    for root in search_roots:
        if not root.is_dir():
            continue
        matches.extend(sorted(root.glob("xAgent-*-linux-*.AppImage")))
    return matches


def _electron_binary(root: Path) -> Optional[Path]:
    if os.name == "nt":
        candidates = (
            root / "node_modules" / ".bin" / "electron.cmd",
            root / "node_modules" / ".bin" / "electron.exe",
        )
    else:
        candidates = (root / "node_modules" / ".bin" / "electron",)

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()

    npx = shutil.which("npx")
    if npx and (root / "package.json").is_file():
        return Path(npx).resolve()
    return None


__all__ = [
    "DESKTOP_APP_ENV",
    "DesktopClientError",
    "desktop_app_dir",
    "desktop_dependencies_ready",
    "desktop_launch_command",
    "desktop_launch_env",
    "desktop_setup_hint",
    "launch_desktop_client",
    "packaged_desktop_app",
]
