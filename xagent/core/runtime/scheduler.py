"""Shared helpers for file-backed scheduled tasks."""
from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path


TASK_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
RUNNING_MARKER = ".running-"
FAILED_DIRNAME = "failed"


def format_task_timestamp(run_at: datetime) -> str:
    """Format a datetime as the scheduler filename timestamp prefix."""
    return run_at.replace(microsecond=0).strftime(TASK_TIMESTAMP_FORMAT)


def parse_run_at(value: str | datetime) -> datetime:
    """Parse user-facing schedule time text into a local naive datetime."""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            raise ValueError("scheduled time is required")
        for fmt in (
            TASK_TIMESTAMP_FORMAT,
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M",
        ):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                parsed = None
        if parsed is None:
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError as exc:
                raise ValueError(
                    "scheduled time must look like YYYYMMDD-HHMMSS or YYYY-MM-DD HH:MM[:SS]"
                ) from exc

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed.replace(microsecond=0)


def ensure_scheduler_dirs(tasks_dir: Path | str) -> tuple[Path, Path]:
    """Ensure the scheduler task root and failed directory exist."""
    root = Path(tasks_dir).expanduser().resolve()
    failed = root / FAILED_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    failed.mkdir(parents=True, exist_ok=True)
    return root, failed


def _fsync_directory(path: Path) -> None:
    try:
        directory_fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        pass
    finally:
        os.close(directory_fd)


def _unique_failed_path(failed_dir: Path, name: str, reason: str) -> Path:
    destination = failed_dir / f"{name}.{reason}"
    if not destination.exists():
        return destination
    for index in range(1, 1000):
        candidate = failed_dir / f"{name}.{reason}.{index}"
        if not candidate.exists():
            return candidate
    return failed_dir / f"{name}.{reason}.{uuid.uuid4().hex[:8]}"
