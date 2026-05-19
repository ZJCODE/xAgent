"""Timezone helpers for canonical UTC timestamps and local display text."""

from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_TIMEZONE = "Asia/Shanghai"
UTC = ZoneInfo("UTC")


def validate_timezone_name(value: str) -> str:
    """Return a normalized IANA timezone name or raise ``ValueError``."""
    name = str(value or "").strip()
    if not name:
        raise ValueError("timezone cannot be empty")
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unsupported timezone: {name}") from exc
    return name


def resolve_timezone(
    config: Mapping[str, Any] | None = None,
    request_timezone: str | None = None,
) -> ZoneInfo:
    """Resolve the active display timezone from request, config, local, then UTC."""
    for candidate in (
        request_timezone,
        _runtime_timezone(config),
        _system_timezone_name(),
        DEFAULT_TIMEZONE,
        "UTC",
    ):
        if not candidate:
            continue
        try:
            return ZoneInfo(str(candidate).strip())
        except ZoneInfoNotFoundError:
            continue
    return UTC


def timezone_name(tz: tzinfo) -> str:
    """Return a stable user-facing timezone name."""
    key = getattr(tz, "key", None)
    if key:
        return str(key)
    return str(tz.tzname(datetime.now(tz)) or "UTC")


def format_in_timezone(unix_seconds: float, tz: tzinfo) -> str:
    """Format a UTC Unix timestamp in the requested timezone with offset."""
    dt = datetime.fromtimestamp(float(unix_seconds), timezone.utc).astimezone(tz)
    return f"{dt.strftime('%Y-%m-%d %H:%M:%S')} {timezone_name(tz)} ({_offset_text(dt)})"


def format_utc(unix_seconds: float) -> str:
    """Format a UTC Unix timestamp explicitly as UTC."""
    dt = datetime.fromtimestamp(float(unix_seconds), timezone.utc)
    return f"{dt.strftime('%Y-%m-%d %H:%M:%S')} UTC (+00:00)"


def current_time_text(tz: tzinfo) -> str:
    """Return the current time in the requested timezone."""
    return format_in_timezone(datetime.now(timezone.utc).timestamp(), tz)


def utc_offset_text(unix_seconds: float, tz: tzinfo) -> str:
    """Return the UTC offset for a timestamp in the requested timezone."""
    dt = datetime.fromtimestamp(float(unix_seconds), timezone.utc).astimezone(tz)
    return _offset_text(dt)


def enrich_timestamp_fields(row: Mapping[str, Any], tz: tzinfo) -> dict[str, Any]:
    """Add UTC/local companions for timestamp-like numeric fields in a result row."""
    enriched = dict(row)
    for key, value in row.items():
        if not _is_timestamp_column(key) or not _is_number(value):
            continue
        enriched[f"{key}_utc"] = format_utc(float(value))
        enriched[f"{key}_local"] = format_in_timezone(float(value), tz)
        enriched[f"{key}_timezone"] = timezone_name(tz)
        enriched[f"{key}_utc_offset"] = utc_offset_text(float(value), tz)
    return enriched


def _runtime_timezone(config: Mapping[str, Any] | None) -> str | None:
    if not isinstance(config, Mapping):
        return None
    runtime = config.get("runtime")
    if not isinstance(runtime, Mapping):
        return None
    value = runtime.get("timezone")
    return str(value).strip() if value else None


def _system_timezone_name() -> str | None:
    local_tz = datetime.now().astimezone().tzinfo
    key = getattr(local_tz, "key", None)
    if key:
        return str(key)
    name = local_tz.tzname(None) if local_tz is not None else None
    return str(name).strip() if name else None


def _offset_text(dt: datetime) -> str:
    offset = dt.utcoffset()
    if offset is None:
        return "+00:00"
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    abs_minutes = abs(total_minutes)
    hours, minutes = divmod(abs_minutes, 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def _is_timestamp_column(column: str) -> bool:
    normalized = str(column or "").lower()
    return normalized in {"timestamp", "created_at", "generated_at", "observed_at"} or normalized.endswith("_timestamp")


def _is_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float))
