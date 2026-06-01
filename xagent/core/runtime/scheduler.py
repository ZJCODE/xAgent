"""File-backed shell task scheduler."""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional


TASK_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
TASK_NAME_RE = re.compile(r"^(?P<stamp>\d{8}-\d{6})(?:-[A-Za-z0-9]{4,32})?\.sh$")
RUNNING_MARKER = ".running-"
FAILED_DIRNAME = "failed"
DEFAULT_TASK_TIMEOUT_SECONDS = 300
DEFAULT_POLL_INTERVAL_SECONDS = 1.0


@dataclass(frozen=True)
class ScheduledTask:
    """A pending scheduler task discovered from the filesystem."""

    path: Path
    run_at: datetime
    command: str = ""

    @property
    def name(self) -> str:
        return self.path.name


@dataclass(frozen=True)
class SchedulerTick:
    """Summary of one scheduler scan."""

    dispatched: int
    next_run_at: Optional[datetime]


def parse_task_filename(name: str) -> Optional[datetime]:
    """Parse a scheduler task filename into a local datetime."""
    match = TASK_NAME_RE.match(name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group("stamp"), TASK_TIMESTAMP_FORMAT)
    except ValueError:
        return None


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


def enqueue_command(command: str, run_at: str | datetime, tasks_dir: Path | str) -> ScheduledTask:
    """Atomically enqueue a shell command as a timestamp-named task file."""
    stripped_command = command.strip()
    if not stripped_command:
        raise ValueError("scheduled command must not be empty")

    parsed_run_at = parse_run_at(run_at)
    root, _failed = ensure_scheduler_dirs(tasks_dir)
    stamp = format_task_timestamp(parsed_run_at)
    temp_path = root / f".{stamp}-{uuid.uuid4().hex}.tmp"
    candidates = [f"{stamp}.sh"]
    candidates.extend(f"{stamp}-{uuid.uuid4().hex[:8]}.sh" for _ in range(32))

    with temp_path.open("w", encoding="utf-8") as handle:
        handle.write(stripped_command)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())

    try:
        for candidate in candidates:
            final_path = root / candidate
            try:
                os.link(temp_path, final_path)
            except FileExistsError:
                continue
            _fsync_directory(root)
            return ScheduledTask(path=final_path, run_at=parsed_run_at, command=stripped_command)
        raise FileExistsError(f"could not reserve a unique task filename for {stamp}")
    finally:
        temp_path.unlink(missing_ok=True)


def list_scheduled_tasks(tasks_dir: Path | str, *, include_commands: bool = False) -> list[ScheduledTask]:
    """Return pending .sh tasks sorted by their filesystem names."""
    root = Path(tasks_dir).expanduser().resolve()
    if not root.is_dir():
        return []

    tasks: list[ScheduledTask] = []
    for path in sorted(root.glob("*.sh"), key=lambda item: item.name):
        run_at = parse_task_filename(path.name)
        if run_at is None:
            continue
        command = ""
        if include_commands:
            try:
                command = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                command = ""
        tasks.append(ScheduledTask(path=path, run_at=run_at, command=command))
    return tasks


def _unique_failed_path(failed_dir: Path, name: str, reason: str) -> Path:
    destination = failed_dir / f"{name}.{reason}"
    if not destination.exists():
        return destination
    for index in range(1, 1000):
        candidate = failed_dir / f"{name}.{reason}.{index}"
        if not candidate.exists():
            return candidate
    return failed_dir / f"{name}.{reason}.{uuid.uuid4().hex[:8]}"


class FileScheduler:
    """Consume timestamp-named shell task files from a directory."""

    def __init__(
        self,
        tasks_dir: Path | str,
        *,
        working_directory: Path | str | None = None,
        timeout_seconds: int = DEFAULT_TASK_TIMEOUT_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        logger_: logging.Logger | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")

        self.tasks_dir, self.failed_dir = ensure_scheduler_dirs(tasks_dir)
        self.working_directory = Path(working_directory).expanduser().resolve() if working_directory else None
        if self.working_directory is not None:
            self.working_directory.mkdir(parents=True, exist_ok=True)
        self.timeout_seconds = int(timeout_seconds)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.logger = logger_ or logging.getLogger(__name__)
        self.now_provider = now_provider or datetime.now
        self._stop_event = threading.Event()

    def request_stop(self) -> None:
        """Ask the foreground scheduler loop to stop."""
        self._stop_event.set()

    def recover_running_tasks(self) -> int:
        """Requeue tasks left in claimed state by a previous interrupted process."""
        recovered = 0
        for path in sorted(self.tasks_dir.glob(f"*.sh{RUNNING_MARKER}*"), key=lambda item: item.name):
            original_name = path.name.split(RUNNING_MARKER, 1)[0]
            if parse_task_filename(original_name) is None:
                self._quarantine(path, path.name, "invalid")
                continue
            destination = self.tasks_dir / original_name
            if destination.exists():
                self._quarantine(path, original_name, "orphaned")
                continue
            try:
                path.rename(destination)
            except OSError as exc:
                self.logger.error("failed to recover running task %s: %s", path.name, exc)
                continue
            recovered += 1
            self.logger.warning("recovered interrupted scheduled task -> %s", original_name)
        return recovered

    def tick(self, *, wait: bool = False) -> SchedulerTick:
        """Scan once, claim due tasks, and dispatch them in daemon threads."""
        now = self.now_provider()
        next_run_at: Optional[datetime] = None
        threads: list[threading.Thread] = []

        for task in list_scheduled_tasks(self.tasks_dir):
            if task.run_at <= now:
                claimed_path = self._claim(task.path)
                if claimed_path is None:
                    continue
                thread = threading.Thread(
                    target=self._execute_task,
                    args=(claimed_path, task.name),
                    daemon=True,
                )
                thread.start()
                threads.append(thread)
                continue

            if next_run_at is None or task.run_at < next_run_at:
                next_run_at = task.run_at

        if wait:
            for thread in threads:
                thread.join()
        return SchedulerTick(dispatched=len(threads), next_run_at=next_run_at)

    def sleep_duration(self, next_run_at: Optional[datetime]) -> float:
        """Compute a bounded sleep interval for the next loop."""
        if next_run_at is None:
            return self.poll_interval_seconds
        delay = (next_run_at - self.now_provider()).total_seconds()
        if delay <= 0:
            return 0.0
        return min(delay, self.poll_interval_seconds)

    def run_forever(self) -> None:
        """Run the scheduler loop until request_stop or a process signal stops it."""
        self.logger.info(
            "file scheduler started: tasks=%s cwd=%s timeout=%ss poll=%ss",
            self.tasks_dir,
            self.working_directory or "(inherit)",
            self.timeout_seconds,
            self.poll_interval_seconds,
        )
        self.recover_running_tasks()
        while not self._stop_event.is_set():
            try:
                tick = self.tick()
                self._stop_event.wait(self.sleep_duration(tick.next_run_at))
            except Exception as exc:
                self.logger.exception("scheduler loop error: %s", exc)
                self._stop_event.wait(self.poll_interval_seconds)
        self.logger.info("file scheduler stopped")

    def _claim(self, path: Path) -> Optional[Path]:
        for _attempt in range(8):
            claimed_path = path.with_name(f"{path.name}{RUNNING_MARKER}{uuid.uuid4().hex[:8]}")
            if claimed_path.exists():
                continue
            try:
                path.rename(claimed_path)
                return claimed_path
            except FileNotFoundError:
                return None
            except OSError as exc:
                self.logger.error("failed to claim scheduled task %s: %s", path.name, exc)
                return None
        self.logger.error("failed to claim scheduled task %s: could not reserve running name", path.name)
        return None

    def _execute_task(self, claimed_path: Path, original_name: str) -> None:
        try:
            command = claimed_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            self.logger.error("failed to read scheduled task %s: %s", original_name, exc)
            return

        if not command:
            self.logger.warning("empty scheduled task skipped -> %s", original_name)
            claimed_path.unlink(missing_ok=True)
            return

        self.logger.info("scheduled task started -> %s", original_name)
        try:
            subprocess.run(
                command,
                shell=True,
                check=True,
                timeout=self.timeout_seconds,
                cwd=str(self.working_directory) if self.working_directory else None,
            )
        except subprocess.TimeoutExpired:
            self.logger.error("scheduled task timed out after %ss -> %s", self.timeout_seconds, original_name)
            self._quarantine(claimed_path, original_name, "timeout")
            return
        except subprocess.CalledProcessError as exc:
            self.logger.error("scheduled task failed return_code=%s -> %s", exc.returncode, original_name)
            self._quarantine(claimed_path, original_name, "failed")
            return
        except Exception as exc:
            self.logger.error("scheduled task error: %s -> %s", exc, original_name)
            self._quarantine(claimed_path, original_name, "error")
            return

        self.logger.info("scheduled task completed -> %s", original_name)
        claimed_path.unlink(missing_ok=True)

    def _quarantine(self, path: Path, original_name: str, reason: str) -> None:
        self.failed_dir.mkdir(parents=True, exist_ok=True)
        destination = _unique_failed_path(self.failed_dir, original_name, reason)
        try:
            path.rename(destination)
        except FileNotFoundError:
            return
        except OSError as exc:
            self.logger.error("failed to quarantine scheduled task %s: %s", original_name, exc)
            return
        self.logger.info("scheduled task quarantined -> %s", destination.name)
