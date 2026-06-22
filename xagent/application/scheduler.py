"""Shared helpers for file-backed scheduled tasks."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Mapping


TASK_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
RUNNING_MARKER = ".running-"
FAILED_DIRNAME = "failed"
WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_INDEX_BY_NAME = {name: index for index, name in enumerate(WEEKDAY_NAMES)}
WEEKDAY_ALIASES = {
    "mon": "mon",
    "monday": "mon",
    "tue": "tue",
    "tues": "tue",
    "tuesday": "tue",
    "wed": "wed",
    "wednesday": "wed",
    "thu": "thu",
    "thur": "thu",
    "thurs": "thu",
    "thursday": "thu",
    "fri": "fri",
    "friday": "fri",
    "sat": "sat",
    "saturday": "sat",
    "sun": "sun",
    "sunday": "sun",
}
RECURRENCE_KIND_DAILY = "daily"
RECURRENCE_KIND_WEEKLY = "weekly"


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


def parse_time_of_day(value: str) -> time:
    """Parse a local wall-clock time such as HH:MM or HH:MM:SS."""
    text = str(value or "").strip()
    if not text:
        raise ValueError("recurring tasks require run_at like HH:MM[:SS]")
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time().replace(microsecond=0)
        except ValueError:
            continue
    raise ValueError("recurring tasks require run_at like HH:MM[:SS]")


def format_time_of_day(value: time) -> str:
    """Format a local wall-clock time as HH:MM:SS."""
    return value.replace(microsecond=0).strftime("%H:%M:%S")


def normalize_weekday(value: str) -> str:
    """Normalize a weekday name into the canonical mon..sun form."""
    normalized = str(value or "").strip().lower()
    canonical = WEEKDAY_ALIASES.get(normalized)
    if canonical is None:
        raise ValueError(f"weekday must be one of: {', '.join(WEEKDAY_NAMES)}")
    return canonical


def normalize_weekdays(value: str | list[str] | tuple[str, ...] | set[str] | None) -> list[str]:
    """Normalize weekday input into a deduplicated, canonical weekday list."""
    raw_values: list[str] = []
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        raw_values = [part.strip() for part in text.split(",") if part.strip()]
    elif isinstance(value, (list, tuple, set)):
        raw_values = [str(item).strip() for item in value if str(item).strip()]
    else:
        raise ValueError("weekdays must be a string or list of weekday names")

    normalized = {normalize_weekday(item) for item in raw_values}
    return [name for name in WEEKDAY_NAMES if name in normalized]


def normalize_recurrence_rules(value: Any) -> list[dict[str, Any]]:
    """Normalize recurrence input into a deterministic list of structured rules."""
    if value is None:
        return []

    raw_rules: list[Any]
    if isinstance(value, Mapping):
        raw_rules = [value]
    elif isinstance(value, (list, tuple)):
        raw_rules = list(value)
    else:
        raise ValueError("recurrence must be an object or a list of objects")

    normalized_rules: list[dict[str, Any]] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    for raw_rule in raw_rules:
        if not isinstance(raw_rule, Mapping):
            raise ValueError("each recurrence rule must be an object")
        kind = str(raw_rule.get("kind") or "").strip().lower()
        if kind not in {RECURRENCE_KIND_DAILY, RECURRENCE_KIND_WEEKLY}:
            raise ValueError(
                f"recurrence rule kind must be one of: {RECURRENCE_KIND_DAILY}, {RECURRENCE_KIND_WEEKLY}"
            )
        formatted_time = format_time_of_day(parse_time_of_day(str(raw_rule.get("time") or "")))
        if kind == RECURRENCE_KIND_DAILY:
            rule: dict[str, Any] = {"kind": RECURRENCE_KIND_DAILY, "time": formatted_time}
            key = (kind, formatted_time, ())
        else:
            weekdays = normalize_weekdays(raw_rule.get("weekdays"))
            if not weekdays:
                raise ValueError("weekly recurrence rules require weekdays like ['wed']")
            rule = {
                "kind": RECURRENCE_KIND_WEEKLY,
                "time": formatted_time,
                "weekdays": weekdays,
            }
            key = (kind, formatted_time, tuple(weekdays))
        if key in seen:
            continue
        seen.add(key)
        normalized_rules.append(rule)
    return sorted(normalized_rules, key=_recurrence_rule_sort_key)


def resolve_daily_run_at(value: str, *, now: datetime | None = None) -> datetime:
    """Resolve a local daily wall-clock time into the next future datetime."""
    current = (now or datetime.now()).replace(microsecond=0)
    parsed_time = parse_time_of_day(value)
    candidate = current.replace(
        hour=parsed_time.hour,
        minute=parsed_time.minute,
        second=parsed_time.second,
        microsecond=0,
    )
    if candidate <= current:
        candidate += timedelta(days=1)
    return candidate


def calculate_next_daily_run_at(value: datetime, *, now: datetime | None = None) -> datetime:
    """Advance a daily recurring task to the next future wall-clock occurrence."""
    current = (now or datetime.now()).replace(microsecond=0)
    candidate = current.replace(
        hour=value.hour,
        minute=value.minute,
        second=value.second,
        microsecond=0,
    )
    if candidate <= current:
        candidate += timedelta(days=1)
    return candidate


def resolve_weekly_run_at(
    value: str,
    *,
    weekdays: str | list[str] | tuple[str, ...] | set[str],
    now: datetime | None = None,
) -> datetime:
    """Resolve a local weekly wall-clock time into the next future datetime."""
    parsed_weekdays = normalize_weekdays(weekdays)
    if not parsed_weekdays:
        raise ValueError("weekly recurring tasks require weekdays like ['wed']")
    return _next_weekly_occurrence(parse_time_of_day(value), weekdays=parsed_weekdays, now=now)


def calculate_next_weekly_run_at(
    value: datetime,
    *,
    weekdays: str | list[str] | tuple[str, ...] | set[str],
    now: datetime | None = None,
) -> datetime:
    """Advance a weekly recurring task to the next future matching weekday/time."""
    parsed_weekdays = normalize_weekdays(weekdays)
    if not parsed_weekdays:
        raise ValueError("weekly recurring tasks require weekdays like ['wed']")
    return _next_weekly_occurrence(value.time().replace(microsecond=0), weekdays=parsed_weekdays, now=now)


def resolve_recurrence_run_at(value: Any, *, now: datetime | None = None) -> datetime:
    """Resolve recurrence rules into the next future scheduled datetime."""
    rules = normalize_recurrence_rules(value)
    if not rules:
        raise ValueError("recurrence rules are required")
    current = (now or datetime.now()).replace(microsecond=0)
    candidates = [_next_occurrence_for_rule(rule, now=current) for rule in rules]
    return min(candidates)


def calculate_next_recurrence_run_at(value: Any, *, now: datetime | None = None) -> datetime:
    """Advance recurrence rules to the next future scheduled datetime."""
    return resolve_recurrence_run_at(value, now=now)


def _next_weekly_occurrence(
    value: time,
    *,
    weekdays: list[str],
    now: datetime | None = None,
) -> datetime:
    current = (now or datetime.now()).replace(microsecond=0)
    candidates: list[datetime] = []
    for weekday in weekdays:
        days_ahead = (WEEKDAY_INDEX_BY_NAME[weekday] - current.weekday()) % 7
        candidate = current.replace(
            hour=value.hour,
            minute=value.minute,
            second=value.second,
            microsecond=0,
        ) + timedelta(days=days_ahead)
        if candidate <= current:
            candidate += timedelta(days=7)
        candidates.append(candidate)
    return min(candidates)


def _next_occurrence_for_rule(rule: Mapping[str, Any], *, now: datetime | None = None) -> datetime:
    current = (now or datetime.now()).replace(microsecond=0)
    kind = str(rule.get("kind") or "").strip().lower()
    time_value = str(rule.get("time") or "")
    if kind == RECURRENCE_KIND_DAILY:
        return resolve_daily_run_at(time_value, now=current)
    if kind == RECURRENCE_KIND_WEEKLY:
        return resolve_weekly_run_at(time_value, weekdays=rule.get("weekdays"), now=current)
    raise ValueError(f"unsupported recurrence rule kind: {kind}")


def _recurrence_rule_sort_key(rule: Mapping[str, Any]) -> tuple[str, str, tuple[str, ...]]:
    kind = str(rule.get("kind") or "").strip().lower()
    time_value = str(rule.get("time") or "").strip()
    weekdays = tuple(normalize_weekdays(rule.get("weekdays")))
    return kind, time_value, weekdays


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
