from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from driving_log.config import DEFAULT_TIMEZONE
from driving_log.solar import split_day_night


@dataclass(frozen=True)
class ValidatedInterval:
    started_at_utc: datetime
    ended_at_utc: datetime
    duration_minutes: int
    day_minutes: int
    night_minutes: int
    warnings: tuple[str, ...]


def validate_interval(
    started_at_utc: datetime,
    ended_at_utc: datetime,
    *,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> ValidatedInterval:
    seconds = (ended_at_utc - started_at_utc).total_seconds()
    if seconds <= 0:
        raise ValueError("drive duration must be positive")
    if (
        started_at_utc.second
        or started_at_utc.microsecond
        or ended_at_utc.second
        or ended_at_utc.microsecond
    ):
        raise ValueError("drive timestamps must be minute-aligned")
    duration = int(seconds // 60)
    day, night = split_day_night(started_at_utc, ended_at_utc, timezone_name=timezone_name)
    warnings: list[str] = []
    if duration > 300:
        warnings.append("long_drive")
    zone = ZoneInfo(timezone_name)
    if started_at_utc.astimezone(zone).date() != ended_at_utc.astimezone(zone).date():
        warnings.append("crosses_midnight")
    return ValidatedInterval(started_at_utc, ended_at_utc, duration, day, night, tuple(warnings))


def validate_road_type(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in {"local", "highway", "mixed", "unknown"}:
        raise ValueError("road type must be local, highway, mixed, or unknown")
    return normalized
