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
RECURRENCE_KIND_INTERVAL = "interval"
MIN_INTERVAL_EVERY_SECONDS = 60
MAX_INTERVAL_DURATION_SECONDS = 30 * 24 * 3600


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
    seen: set[tuple[str, str, tuple[str, ...] | int]] = set()
    for raw_rule in raw_rules:
        if not isinstance(raw_rule, Mapping):
            raise ValueError("each recurrence rule must be an object")
        kind = str(raw_rule.get("kind") or "").strip().lower()
        if kind not in {RECURRENCE_KIND_DAILY, RECURRENCE_KIND_WEEKLY, RECURRENCE_KIND_INTERVAL}:
            raise ValueError(
                f"recurrence rule kind must be one of: {RECURRENCE_KIND_DAILY}, {RECURRENCE_KIND_WEEKLY}, {RECURRENCE_KIND_INTERVAL}"
            )
        if kind == RECURRENCE_KIND_INTERVAL:
            rule = _normalize_interval_rule(raw_rule)
            key = (kind, str(rule["end_at"]), int(rule["every_seconds"]))
            if key in seen:
                continue
            seen.add(key)
            normalized_rules.append(rule)
            continue
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
    _validate_recurrence_rule_mix(normalized_rules)
    return sorted(normalized_rules, key=_recurrence_rule_sort_key)


def materialize_interval_recurrence_rules(value: Any, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Normalize recurrence rules and convert interval duration into an absolute end_at."""
    current = (now or datetime.now()).replace(microsecond=0)
    rules = _raw_recurrence_rules(value)
    materialized: list[dict[str, Any]] = []
    for raw_rule in rules:
        if not isinstance(raw_rule, Mapping):
            raise ValueError("each recurrence rule must be an object")
        rule = dict(raw_rule)
        kind = str(rule.get("kind") or "").strip().lower()
        if kind == RECURRENCE_KIND_INTERVAL and "duration_seconds" in rule and "end_at" not in rule:
            duration_seconds = _parse_positive_int(rule.get("duration_seconds"), "duration_seconds")
            if duration_seconds > MAX_INTERVAL_DURATION_SECONDS:
                raise ValueError(
                    f"duration_seconds must be at most {MAX_INTERVAL_DURATION_SECONDS} for interval recurrence"
                )
            rule["end_at"] = (current + timedelta(seconds=duration_seconds)).isoformat(sep=" ")
            rule.pop("duration_seconds", None)
        materialized.append(rule)
    return normalize_recurrence_rules(materialized)


def is_interval_recurrence(value: Any) -> bool:
    rules = normalize_recurrence_rules(value)
    return len(rules) == 1 and rules[0].get("kind") == RECURRENCE_KIND_INTERVAL


def resolve_interval_first_run_at(
    rule: Mapping[str, Any],
    *,
    now: datetime | None = None,
    delay_seconds: int | None = None,
) -> datetime:
    current = (now or datetime.now()).replace(microsecond=0)
    normalized = _normalize_interval_rule(rule)
    if delay_seconds is not None:
        if delay_seconds < 0:
            raise ValueError("delay_seconds must be zero or positive.")
        candidate = current + timedelta(seconds=delay_seconds)
    else:
        candidate = current + timedelta(seconds=int(normalized["every_seconds"]))
    end_at = parse_run_at(str(normalized["end_at"]))
    if candidate > end_at:
        raise ValueError("duration is too short for even one interval execution")
    return candidate


def calculate_next_interval_run_at(
    current_run_at: datetime,
    rule: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> datetime | None:
    normalized = _normalize_interval_rule(rule)
    next_run_at = parse_run_at(current_run_at) + timedelta(seconds=int(normalized["every_seconds"]))
    end_at = parse_run_at(str(normalized["end_at"]))
    if next_run_at > end_at:
        return None
    return next_run_at


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


def calculate_next_recurrence_run_at(
    value: Any,
    *,
    now: datetime | None = None,
    current_run_at: datetime | None = None,
) -> datetime | None:
    """Advance recurrence rules to the next future scheduled datetime."""
    rules = normalize_recurrence_rules(value)
    if is_interval_recurrence(rules):
        if current_run_at is None:
            raise ValueError("current_run_at is required for interval recurrence")
        return calculate_next_interval_run_at(current_run_at, rules[0], now=now)
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
    if kind == RECURRENCE_KIND_INTERVAL:
        return resolve_interval_first_run_at(rule, now=current)
    raise ValueError(f"unsupported recurrence rule kind: {kind}")


def _recurrence_rule_sort_key(rule: Mapping[str, Any]) -> tuple[str, str, tuple[str, ...]]:
    kind = str(rule.get("kind") or "").strip().lower()
    if kind == RECURRENCE_KIND_INTERVAL:
        return kind, str(rule.get("end_at") or ""), (str(rule.get("every_seconds") or ""),)
    time_value = str(rule.get("time") or "").strip()
    weekdays = tuple(normalize_weekdays(rule.get("weekdays")))
    return kind, time_value, weekdays


def _raw_recurrence_rules(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    raise ValueError("recurrence must be an object or a list of objects")


def _normalize_interval_rule(raw_rule: Mapping[str, Any]) -> dict[str, Any]:
    every_seconds = _parse_positive_int(raw_rule.get("every_seconds"), "every_seconds")
    if every_seconds < MIN_INTERVAL_EVERY_SECONDS:
        raise ValueError(f"every_seconds must be at least {MIN_INTERVAL_EVERY_SECONDS} for interval recurrence")
    has_end_at = bool(str(raw_rule.get("end_at") or "").strip())
    has_duration = "duration_seconds" in raw_rule and raw_rule.get("duration_seconds") is not None
    if has_end_at == has_duration:
        raise ValueError(
            "interval recurrence requires exactly one user-provided end_at or duration_seconds; "
            "ask the user how long to continue or when to stop before creating"
        )
    if has_duration:
        raise ValueError("duration_seconds must be materialized to end_at before storing interval recurrence")
    end_at = parse_run_at(str(raw_rule.get("end_at") or ""))
    return {
        "kind": RECURRENCE_KIND_INTERVAL,
        "every_seconds": every_seconds,
        "end_at": end_at.isoformat(sep=" "),
    }


def _parse_positive_int(value: Any, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _validate_recurrence_rule_mix(rules: list[dict[str, Any]]) -> None:
    interval_count = sum(1 for rule in rules if rule.get("kind") == RECURRENCE_KIND_INTERVAL)
    if interval_count and len(rules) != 1:
        raise ValueError("interval recurrence cannot be combined with other recurrence rules")


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
