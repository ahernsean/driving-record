from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from driving_log.config import APEX_LATITUDE, APEX_LONGITUDE, DEFAULT_TIMEZONE

SOLAR_CALCULATION_VERSION = "apex-astral-3.2-v1"


class LocalTimeError(ValueError):
    pass


class NonexistentLocalTimeError(LocalTimeError):
    pass


class AmbiguousLocalTimeError(LocalTimeError):
    pass


def resolve_local(
    value: datetime, timezone_name: str = DEFAULT_TIMEZONE, fold: int | None = None
) -> datetime:
    if value.tzinfo is not None:
        raise LocalTimeError("expected a naive local datetime")
    zone = ZoneInfo(timezone_name)
    candidates: list[datetime] = []
    for candidate_fold in (0, 1):
        candidate = value.replace(tzinfo=zone, fold=candidate_fold)
        round_trip = candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
        if round_trip == value:
            candidates.append(candidate)
    unique_offsets = {candidate.utcoffset() for candidate in candidates}
    if not candidates:
        raise NonexistentLocalTimeError(f"{value.isoformat()} does not exist in {timezone_name}")
    if len(unique_offsets) > 1:
        if fold is None:
            raise AmbiguousLocalTimeError(
                f"{value.isoformat()} occurs twice in {timezone_name}; select fold 0 or 1"
            )
        if fold not in (0, 1):
            raise LocalTimeError("fold must be 0 or 1")
        return value.replace(tzinfo=zone, fold=fold)
    return candidates[0]


def apex_daylight_window(local_date: date) -> tuple[datetime, datetime]:
    try:
        from astral import LocationInfo
        from astral.sun import sun
    except ImportError as exc:
        raise RuntimeError("Astral is required for solar calculations") from exc
    zone = ZoneInfo(DEFAULT_TIMEZONE)
    location = LocationInfo("Apex", "USA", DEFAULT_TIMEZONE, APEX_LATITUDE, APEX_LONGITUDE)
    events = sun(location.observer, date=local_date, tzinfo=zone)
    return events["sunrise"] - timedelta(minutes=15), events["sunset"] + timedelta(minutes=15)


def split_day_night(
    started_at_utc: datetime,
    ended_at_utc: datetime,
    *,
    timezone_name: str = DEFAULT_TIMEZONE,
    daylight_window: Callable[[date], tuple[datetime, datetime]] = apex_daylight_window,
) -> tuple[int, int]:
    if started_at_utc.tzinfo is None or ended_at_utc.tzinfo is None:
        raise ValueError("UTC instants must be timezone-aware")
    start = started_at_utc.astimezone(UTC)
    end = ended_at_utc.astimezone(UTC)
    if end <= start:
        raise ValueError("end must be after start")
    zone = ZoneInfo(timezone_name)
    first_date = start.astimezone(zone).date()
    last_date = (end - timedelta(microseconds=1)).astimezone(zone).date()
    day_seconds = 0.0
    current_date = first_date
    while current_date <= last_date:
        local_day_start = datetime.combine(current_date, time.min, zone).astimezone(UTC)
        local_day_end = datetime.combine(
            current_date + timedelta(days=1), time.min, zone
        ).astimezone(UTC)
        segment_start = max(start, local_day_start)
        segment_end = min(end, local_day_end)
        if segment_end > segment_start:
            daylight_start, daylight_end = daylight_window(current_date)
            overlap_start = max(segment_start, daylight_start.astimezone(UTC))
            overlap_end = min(segment_end, daylight_end.astimezone(UTC))
            if overlap_end > overlap_start:
                day_seconds += (overlap_end - overlap_start).total_seconds()
        current_date += timedelta(days=1)
    total_minutes = round((end - start).total_seconds() / 60)
    day_minutes = round(day_seconds / 60)
    return day_minutes, total_minutes - day_minutes


def week_segments(
    started_at_utc: datetime,
    ended_at_utc: datetime,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> list[tuple[datetime, datetime, int]]:
    zone = ZoneInfo(timezone_name)
    start = started_at_utc.astimezone(UTC)
    end = ended_at_utc.astimezone(UTC)
    if end <= start:
        raise ValueError("end must be after start")
    local_start = start.astimezone(zone)
    days_since_sunday = (local_start.weekday() + 1) % 7
    boundary_date = local_start.date() - timedelta(days=days_since_sunday)
    boundary = datetime.combine(boundary_date, time.min, zone).astimezone(UTC)
    segments: list[tuple[datetime, datetime, int]] = []
    while boundary < end:
        next_local_date = boundary.astimezone(zone).date() + timedelta(days=7)
        next_boundary = datetime.combine(next_local_date, time.min, zone).astimezone(UTC)
        segment_start = max(start, boundary)
        segment_end = min(end, next_boundary)
        if segment_end > segment_start:
            segments.append(
                (
                    boundary,
                    next_boundary,
                    round((segment_end - segment_start).total_seconds() / 60),
                )
            )
        boundary = next_boundary
    return segments
