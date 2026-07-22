"""Self-update support that preserves xAgent's current installation method."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import shlex
import shutil
import site
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .processes import iter_running_process_refs
from .processes_status import handle_processes_restart


PACKAGE_NAME = "myxagent"
COMMAND_NAME = "xagent"
INSTALLATION_UV_TOOL = "uv-tool"
INSTALLATION_PIP = "pip"


@dataclass(frozen=True)
class DistributionSnapshot:
    """Installed distribution metadata used to classify the current runtime."""

    version: str
    installer: str
    package_root: Path
    metadata_path: Path
    direct_url: dict[str, Any] | None


@dataclass(frozen=True)
class InstallationInfo:
    """A strongly identified installation and the command that owns it."""

    kind: str
    version: str
    package_root: Path
    python: Path
    update_command: tuple[str, ...]


class InstallationDetectionError(RuntimeError):
    """Raised when updating automatically could target the wrong environment."""

    def __init__(self, message: str, *, details: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.details = tuple(details)


def _canonicalize_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _resolved(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser().resolve()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _read_distribution_snapshot() -> DistributionSnapshot:
    try:
        distribution = importlib.metadata.distribution(PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError as exc:
        raise InstallationDetectionError(
            f"The {PACKAGE_NAME} distribution is not installed in the current Python environment."
        ) from exc

    installer = (distribution.read_text("INSTALLER") or "").strip().lower()
    direct_url_text = distribution.read_text("direct_url.json")
    direct_url: dict[str, Any] | None = None
    if direct_url_text:
        try:
            parsed = json.loads(direct_url_text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            direct_url = parsed

    metadata_value = getattr(distribution, "_path", "")
    metadata_path = _resolved(metadata_value) if metadata_value else _resolved(distribution.locate_file(""))
    return DistributionSnapshot(
        version=distribution.version,
        installer=installer,
        package_root=_resolved(distribution.locate_file("")),
        metadata_path=metadata_path,
        direct_url=direct_url,
    )


def _source_install_reason(snapshot: DistributionSnapshot) -> str | None:
    if snapshot.metadata_path.name.endswith(".egg-info"):
        return f"legacy/editable metadata at {snapshot.metadata_path}"

    direct_url = snapshot.direct_url
    if not direct_url:
        return None
    if isinstance(direct_url.get("dir_info"), dict):
        return f"local directory source {direct_url.get('url', '')}".strip()
    if isinstance(direct_url.get("vcs_info"), dict):
        return f"version-control source {direct_url.get('url', '')}".strip()
    url = str(direct_url.get("url") or "")
    if url.startswith("file:"):
        return f"local file source {url}"
    return None


def _run_capture(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(list(command), 127, "", str(exc))


def _find_uv() -> Path | None:
    found = shutil.which("uv")
    if found:
        return _resolved(found)

    executable_name = "uv.exe" if os.name == "nt" else "uv"
    default_candidate = Path.home() / ".local" / "bin" / executable_name
    if default_candidate.is_file() and os.access(default_candidate, os.X_OK):
        return default_candidate.resolve()
    return None


def _uv_tool_is_registered(output: str) -> bool:
    expected = _canonicalize_name(PACKAGE_NAME)
    for line in output.splitlines():
        if not line or line[0].isspace():
            continue
        name = line.split(maxsplit=1)[0]
        if _canonicalize_name(name) == expected:
            return True
    return False


def _detect_uv_tool(snapshot: DistributionSnapshot) -> InstallationInfo | None:
    uv = _find_uv()
    if uv is None:
        if snapshot.installer == "uv":
            raise InstallationDetectionError(
                "This installation was created by uv, but the uv executable could not be found.",
                details=("Install uv again or add it to PATH, then retry.",),
            )
        return None

    tool_dir_result = _run_capture((str(uv), "tool", "dir"))
    if tool_dir_result.returncode != 0 or not tool_dir_result.stdout.strip():
        if snapshot.installer == "uv":
            detail = tool_dir_result.stderr.strip() or "uv tool dir returned no path"
            raise InstallationDetectionError(
                "uv owns this installation, but its tool directory could not be verified.",
                details=(detail,),
            )
        return None

    tool_dir = _resolved(tool_dir_result.stdout.strip().splitlines()[-1])
    current_prefix = _resolved(sys.prefix)
    expected_environment = tool_dir / PACKAGE_NAME
    if os.path.normcase(str(current_prefix)) != os.path.normcase(str(expected_environment.resolve())):
        return None

    list_result = _run_capture((str(uv), "tool", "list"))
    if list_result.returncode != 0 or not _uv_tool_is_registered(list_result.stdout):
        detail = list_result.stderr.strip() or f"{PACKAGE_NAME} is not present in uv tool list"
        raise InstallationDetectionError(
            "The current Python is inside uv's tool directory, but the xAgent tool registration could not be verified.",
            details=(detail,),
        )

    return InstallationInfo(
        kind=INSTALLATION_UV_TOOL,
        version=snapshot.version,
        package_root=snapshot.package_root,
        python=_resolved(sys.executable),
        update_command=(str(uv), "tool", "upgrade", PACKAGE_NAME),
    )


def _pip_user_install(snapshot: DistributionSnapshot) -> bool:
    try:
        user_site = site.getusersitepackages()
    except (AttributeError, RuntimeError):
        return False
    candidates = [user_site] if isinstance(user_site, str) else list(user_site)
    package_root = _resolved(snapshot.package_root)
    return any(_is_relative_to(package_root, _resolved(candidate)) for candidate in candidates if candidate)


def _detect_pip(snapshot: DistributionSnapshot) -> InstallationInfo | None:
    if snapshot.installer != "pip":
        return None

    pip_result = _run_capture((sys.executable, "-m", "pip", "--version"))
    if pip_result.returncode != 0:
        detail = pip_result.stderr.strip() or "python -m pip is unavailable"
        raise InstallationDetectionError(
            "pip metadata was found, but pip is not available in the current Python environment.",
            details=(detail,),
        )

    current_prefix = _resolved(sys.prefix)
    package_root = _resolved(snapshot.package_root)
    in_current_prefix = _is_relative_to(package_root, current_prefix)
    user_install = _pip_user_install(snapshot)
    if not in_current_prefix and not user_install:
        raise InstallationDetectionError(
            "pip metadata was found, but the package location does not belong to the current Python environment.",
        )

    command = [sys.executable, "-m", "pip", "install", "--upgrade"]
    if user_install and not in_current_prefix:
        command.append("--user")
    command.append(PACKAGE_NAME)
    return InstallationInfo(
        kind=INSTALLATION_PIP,
        version=snapshot.version,
        package_root=package_root,
        python=_resolved(sys.executable),
        update_command=tuple(command),
    )


def detect_installation() -> InstallationInfo:
    """Identify the package manager for the currently executing xAgent."""

    snapshot = _read_distribution_snapshot()
    source_reason = _source_install_reason(snapshot)
    if source_reason:
        raise InstallationDetectionError(
            "xAgent is running from a development or source installation.",
            details=(source_reason, "Update the source checkout and reinstall it with your development workflow."),
        )

    uv_tool = _detect_uv_tool(snapshot)
    if uv_tool is not None:
        return uv_tool

    pip_installation = _detect_pip(snapshot)
    if pip_installation is not None:
        return pip_installation

    installer = snapshot.installer or "not recorded"
    raise InstallationDetectionError(
        "Unable to safely determine how this xAgent installation is managed. No changes were made.",
        details=(
            f"Installer metadata: {installer}",
            f"Package location: {snapshot.package_root}",
            f"Current Python: {sys.executable}",
        ),
    )


def _display_command(command: Sequence[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(list(command))
    return shlex.join(command)


def _fresh_installed_version(python: Path) -> str | None:
    code = f"import importlib.metadata as m; print(m.version({PACKAGE_NAME!r}))"
    result = _run_capture((str(python), "-c", code))
    if result.returncode != 0:
        return None
    version = result.stdout.strip().splitlines()
    return version[-1].strip() if version else None


def _restart_running_processes(restart: bool | None) -> int:
    running_refs = list(iter_running_process_refs())
    if not running_refs:
        return 0

    count = len(running_refs)
    noun = "process" if count == 1 else "processes"
    print(f"\n{count} background {noun} are still running with the previous version.")

    should_restart = restart is True
    if restart is None and sys.stdin.isatty() and sys.stdout.isatty():
        try:
            answer = input("Restart them now? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
            print()
        should_restart = answer in {"y", "yes"}

    if not should_restart:
        print(f"Run '{COMMAND_NAME} processes restart' when you are ready to load the new version.")
        return 0

    print("Restarting running xAgent processes...")
    exit_code = handle_processes_restart(argparse.Namespace(json_output=False))
    if exit_code != 0:
        print("xAgent was updated, but one or more background processes failed to restart.")
        return 1
    return 0


def handle_update_worker(args: argparse.Namespace) -> int:
    """Perform the update after the console launcher has been released."""

    try:
        installation = detect_installation()
    except InstallationDetectionError as exc:
        print(f"Error: {exc}")
        for detail in exc.details:
            print(f"  {detail}")
        return 1

    manager_label = "uv tool" if installation.kind == INSTALLATION_UV_TOOL else "pip"
    print(f"Detected installation: {manager_label}")
    print(f"Current version: {installation.version}")
    print(f"Python: {installation.python}")
    print(f"Updating {PACKAGE_NAME}...")

    try:
        result = subprocess.run(list(installation.update_command), check=False)
    except OSError as exc:
        print(f"Error: failed to start the package manager: {exc}")
        print(f"Retry manually with: {_display_command(installation.update_command)}")
        return 1

    if result.returncode != 0:
        print(f"Error: update failed with exit code {result.returncode}.")
        print(f"Retry manually with: {_display_command(installation.update_command)}")
        return 1

    updated_version = _fresh_installed_version(installation.python)
    if updated_version is None:
        print("Error: the update command finished, but the installed xAgent version could not be verified.")
        print(f"Retry manually with: {_display_command(installation.update_command)}")
        return 1

    if updated_version == installation.version:
        print(f"xAgent is already up to date ({updated_version}).")
        return 0

    print(f"Updated xAgent: {installation.version} -> {updated_version}")
    return _restart_running_processes(getattr(args, "restart", None))


def handle_update(args: argparse.Namespace) -> int:
    """Re-enter through Python so the xagent launcher can be safely replaced."""

    command = [sys.executable, "-m", "xagent.interfaces.cli", "_update-worker"]
    restart = getattr(args, "restart", None)
    if restart is True:
        command.append("--restart")
    elif restart is False:
        command.append("--no-restart")

    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execv(sys.executable, command)
    except OSError as exc:
        print(f"Error: could not start the xAgent update worker: {exc}")
        return 1
    return 1  # pragma: no cover - os.execv does not return on success
