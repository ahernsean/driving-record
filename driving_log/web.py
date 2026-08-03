from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData, UploadFile
from starlette.middleware.base import RequestResponseEndpoint

from driving_log.archive import ArchiveSchemaTooNewError, create_archive
from driving_log.config import Settings
from driving_log.csv_backup import export_csv, import_csv
from driving_log.db import Database
from driving_log.dmv import DmvExportService, SupervisorProfileService, mask_license
from driving_log.live import LiveDriveService
from driving_log.records import ConflictError, DriveInput, NotFoundError, RecordService
from driving_log.solar import apex_daylight_window, resolve_local

PACKAGE_DIR = Path(__file__).parent
ZONE = ZoneInfo("America/New_York")


def _parse_local(value: str, fold_text: str | None = None) -> datetime:
    naive = datetime.fromisoformat(value)
    fold = int(fold_text) if fold_text in ("0", "1") else 0
    return resolve_local(naive, fold=fold).astimezone(UTC)


def _format_minutes(minutes: int) -> str:
    return f"{minutes // 60}h {minutes % 60:02d}m"


def _local_datetime(value: str | datetime) -> datetime:
    parsed = (
        datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(ZONE)


def _format_local_datetime(value: str | datetime) -> str:
    local = _local_datetime(value)
    hour = local.strftime("%I").lstrip("0") or "0"
    return (
        f"{local.strftime('%A')}, {local.strftime('%b')} {local.day}, "
        f"{local.year} at {hour}:{local.strftime('%M %p %Z')}"
    )


def _format_local_date(value: str | datetime) -> str:
    local = _local_datetime(value)
    return f"{local.strftime('%A')}, {local.strftime('%b')} {local.day}, {local.year}"


def _format_date_in_zone(value: str | datetime, timezone_name: str) -> str:
    local = _local_datetime_in_zone(value, timezone_name)
    return f"{local.strftime('%A')}, {local.strftime('%b')} {local.day}, {local.year}"


def _local_input_value(value: str | datetime) -> str:
    return _local_datetime(value).strftime("%Y-%m-%dT%H:%M")


def _duration_parts(minutes: int) -> dict[str, int]:
    return {
        "duration_hours": minutes // 60,
        "duration_remainder": minutes % 60,
    }


TIME_OF_DAY_OPTIONS = (
    ("morning", "Morning (dawn–12 PM)"),
    ("afternoon", "Afternoon (12 PM–5 PM)"),
    ("evening", "Evening (5 PM–dusk)"),
    ("night", "Nighttime (dusk–dawn)"),
)
PARTS_OF_DAY = tuple(value for value, _ in TIME_OF_DAY_OPTIONS)
WEATHER_LABELS = (
    ("clear", "Clear"),
    ("rain", "Rain"),
    ("cloudy", "Cloudy"),
    ("wet", "Wet roads"),
    ("fog", "Fog"),
    ("unspecified", "Unspecified"),
)
GROUP_BY_OPTIONS = (
    ("none", "No grouping"),
    ("date", "Date"),
    ("supervisor", "Supervising driver"),
    ("day_night", "Day / night"),
    ("part_of_day", "Time of day"),
    ("road_type", "Road type"),
    ("weather", "Weather"),
    ("duration", "Duration"),
)


def _part_of_day(value: str | datetime, timezone_name: str) -> str:
    local = _local_datetime_in_zone(value, timezone_name)
    solar_local = local.astimezone(ZONE)
    daylight_start, daylight_end = apex_daylight_window(solar_local.date())
    if solar_local < daylight_start or solar_local >= daylight_end:
        return "night"
    if local.hour < 12:
        return "morning"
    if local.hour < 17:
        return "afternoon"
    return "evening"


def _time_of_day_group(row: sqlite3.Row) -> str:
    if int(row["night_minutes"]):
        return "night"
    return _part_of_day(row["started_at_utc"], row["timezone_name"])


def _time_of_day_label(value: str) -> str:
    if value == "night":
        return "Nighttime (dusk–dawn)"
    return value.title()


def _weather_categories(value: str | None) -> tuple[str, ...]:
    normalized = (value or "").casefold()
    categories = []
    for category, terms in (
        ("clear", ("clear",)),
        ("rain", ("rain",)),
        ("cloudy", ("cloud",)),
        ("wet", ("wet",)),
        ("fog", ("fog",)),
    ):
        if any(term in normalized for term in terms):
            categories.append(category)
    return tuple(categories or ["unspecified"])


def _local_datetime_in_zone(value: str | datetime, timezone_name: str) -> datetime:
    parsed = (
        datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(ZoneInfo(timezone_name))


def _duration_bucket(minutes: int) -> str:
    if minutes < 30:
        return "Under 30 minutes"
    if minutes < 60:
        return "30–59 minutes"
    if minutes < 120:
        return "1–2 hours"
    return "2+ hours"


def _parse_filter_date(value: str, field: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid date") from exc


def _parse_filter_minutes(value: str, field: str) -> int | None:
    if not value:
        return None
    try:
        minutes = int(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a whole number of minutes") from exc
    if minutes < 0:
        raise ValueError(f"{field} cannot be negative")
    return minutes


def _drive_group_key(row: sqlite3.Row, group_by: str) -> tuple[str, str]:
    # sqlite rows deliberately remain the source of truth; this is only presentation grouping.
    if group_by == "date":
        local = _local_datetime_in_zone(str(row["started_at_utc"]), str(row["timezone_name"]))
        return (
            local.date().isoformat(),
            _format_date_in_zone(str(row["started_at_utc"]), str(row["timezone_name"])),
        )
    if group_by == "supervisor":
        name = str(row["supervisor_name"] or "").strip()
        return (name.casefold() or "unspecified", name or "Unspecified")
    if group_by == "day_night":
        day = int(row["day_minutes"])
        night = int(row["night_minutes"])
        if day and night:
            return ("mixed", "Day and night")
        return ("day" if day else "night", "Day" if day else "Night")
    if group_by == "part_of_day":
        part = _time_of_day_group(row)
        return (part, _time_of_day_label(part))
    if group_by == "road_type":
        road_type = str(row["road_type"])
        return (road_type, road_type.title())
    if group_by == "weather":
        categories = _weather_categories(row["weather"])
        labels = dict(WEATHER_LABELS)
        return ("-".join(categories), " · ".join(labels[category] for category in categories))
    if group_by == "duration":
        label = _duration_bucket(int(row["duration_minutes"]))
        return (label, label)
    return ("all", "All drives")


def _theme_context(now: datetime | None = None) -> dict[str, object]:
    selected = (now or datetime.now(UTC)).astimezone(UTC)
    local = selected.astimezone(ZONE)
    start, end = apex_daylight_window(local.date())
    theme = "light" if start.astimezone(UTC) <= selected < end.astimezone(UTC) else "dark"
    boundaries: list[str] = []
    for offset in range(3):
        daylight_start, daylight_end = apex_daylight_window(local.date() + timedelta(days=offset))
        for boundary in (daylight_start, daylight_end):
            utc_boundary = boundary.astimezone(UTC)
            if selected < utc_boundary <= selected + timedelta(hours=48):
                boundaries.append(utc_boundary.isoformat().replace("+00:00", "Z"))
    return {
        "theme": theme,
        "theme_boundaries": sorted(boundaries),
        "server_now_utc": selected.isoformat().replace("+00:00", "Z"),
    }


def _actor(request: Request) -> str | None:
    return request.headers.get("Tailscale-User-Login")


def register_web(app: FastAPI, settings: Settings, database: Database) -> None:
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    asset_digest = hashlib.sha256()
    for asset_name in ("app.css", "app.js"):
        asset_digest.update((PACKAGE_DIR / "static" / asset_name).read_bytes())
    asset_version = asset_digest.hexdigest()[:12]
    records = RecordService(database)
    live = LiveDriveService(database)
    profiles = SupervisorProfileService(database)
    dmv = DmvExportService(database)

    @app.middleware("http")
    async def prevent_stale_html(request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def common(request: Request, **values: object) -> dict[str, object]:
        return {
            "request": request,
            "format_minutes": _format_minutes,
            "format_local_datetime": _format_local_datetime,
            "format_local_date": _format_local_date,
            "asset_version": asset_version,
            **_theme_context(),
            **values,
        }

    async def read_form(request: Request) -> FormData:
        return await request.form()

    def redirect(path: str) -> RedirectResponse:
        return RedirectResponse(path, status_code=303)

    @app.exception_handler(ConflictError)
    async def conflict_handler(request: Request, exc: ConflictError) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "error.html",
            common(request, title="Conflict", message=str(exc)),
            status_code=409,
        )

    @app.exception_handler(NotFoundError)
    async def not_found_handler(request: Request, exc: NotFoundError) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "error.html",
            common(request, title="Not found", message=str(exc)),
            status_code=404,
        )

    @app.exception_handler(ValueError)
    @app.exception_handler(KeyError)
    async def invalid_form_handler(request: Request, exc: ValueError | KeyError) -> HTMLResponse:
        message = (
            f"Missing required field: {exc.args[0]}" if isinstance(exc, KeyError) else str(exc)
        )
        return templates.TemplateResponse(
            request,
            "error.html",
            common(request, title="Invalid form", message=message),
            status_code=400,
        )

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        totals = records.totals()
        open_drive = live.current()
        weeks = cast(list[dict[str, object]], totals["weeks"])
        overages = [
            week
            for week in weeks
            if isinstance(week["overage_minutes"], int) and week["overage_minutes"] > 0
        ]
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            common(
                request,
                title="Daniel Driving Log",
                totals=totals,
                overages=overages,
                live=open_drive,
            ),
        )

    @app.get("/drives", response_class=HTMLResponse)
    async def drive_list(request: Request) -> HTMLResponse:
        raw_filters = {
            name: request.query_params.get(name, "")
            for name in (
                "period",
                "start_date",
                "end_date",
                "supervisor",
                "day_night",
                "part_of_day",
                "road_type",
                "min_duration",
                "max_duration",
                "warnings_only",
                "group_by",
            )
        }
        weather_filters = tuple(request.query_params.getlist("weather"))
        period = raw_filters["period"] or "all"
        if period not in {
            "all",
            "today",
            "week",
            "last_week",
            "month",
            "last_month",
            "year",
            "last_year",
            "custom",
        }:
            raise ValueError("Choose a valid date range")
        group_by = raw_filters["group_by"] or "none"
        if group_by not in {value for value, _ in GROUP_BY_OPTIONS}:
            raise ValueError("Choose a valid grouping")
        if raw_filters["day_night"] not in {"", "day", "night"}:
            raise ValueError("Choose day or night")
        if raw_filters["warnings_only"] not in {"", "1"}:
            raise ValueError("Choose whether to show warnings only")
        if raw_filters["part_of_day"] not in {"", *PARTS_OF_DAY}:
            raise ValueError("Choose a valid time of day")
        if raw_filters["supervisor"] not in {"", "none"}:
            known_supervisors = {
                str(row["supervisor_name"])
                for row in records.list_drives()
                if row["supervisor_name"]
            }
            if raw_filters["supervisor"] not in known_supervisors:
                raise ValueError("Choose a valid supervising driver")
        weather_keys = {value for value, _ in WEATHER_LABELS}
        if any(weather not in weather_keys for weather in weather_filters):
            raise ValueError("Choose valid weather conditions")

        today = datetime.now(ZONE).date()
        start_date: date | None = None
        end_date: date | None = None
        if period == "custom":
            start_date = _parse_filter_date(raw_filters["start_date"], "Start date")
            end_date = _parse_filter_date(raw_filters["end_date"], "End date")
            if start_date and end_date and start_date > end_date:
                raise ValueError("Start date must not be after end date")
        elif period != "all":
            if period == "today":
                start_date = end_date = today
            elif period == "week":
                start_date, end_date = today - timedelta(days=today.weekday()), today
            elif period == "last_week":
                end_date = today - timedelta(days=today.weekday() + 1)
                start_date = end_date - timedelta(days=6)
            elif period == "month":
                start_date, end_date = today.replace(day=1), today
            elif period == "last_month":
                end_date = today.replace(day=1) - timedelta(days=1)
                start_date = end_date.replace(day=1)
            elif period == "year":
                start_date, end_date = today.replace(month=1, day=1), today
            elif period == "last_year":
                start_date = today.replace(year=today.year - 1, month=1, day=1)
                end_date = today.replace(year=today.year - 1, month=12, day=31)
            if start_date and end_date:
                raw_filters["start_date"] = start_date.isoformat()
                raw_filters["end_date"] = end_date.isoformat()
        else:
            raw_filters["start_date"] = ""
            raw_filters["end_date"] = ""
        min_duration = _parse_filter_minutes(raw_filters["min_duration"], "Minimum duration")
        max_duration = _parse_filter_minutes(raw_filters["max_duration"], "Maximum duration")
        if min_duration is not None and max_duration is not None and min_duration > max_duration:
            raise ValueError("Minimum duration must not exceed maximum duration")

        warnings = records.warnings_for_many()
        drives = []
        for row in records.list_drives():
            local = _local_datetime_in_zone(row["started_at_utc"], row["timezone_name"])
            if start_date and local.date() < start_date:
                continue
            if end_date and local.date() > end_date:
                continue
            if raw_filters["supervisor"] == "none" and row["supervisor_name"]:
                continue
            if raw_filters["supervisor"] not in {"", "none"} and (
                row["supervisor_name"] != raw_filters["supervisor"]
            ):
                continue
            if raw_filters["day_night"] == "day" and not row["day_minutes"]:
                continue
            if raw_filters["day_night"] == "night" and not row["night_minutes"]:
                continue
            selected_part = raw_filters["part_of_day"]
            if selected_part and (
                _part_of_day(row["started_at_utc"], row["timezone_name"]) != selected_part
            ):
                continue
            if raw_filters["road_type"] and row["road_type"] != raw_filters["road_type"]:
                continue
            if weather_filters and not set(weather_filters).intersection(
                _weather_categories(row["weather"])
            ):
                continue
            if min_duration is not None and row["duration_minutes"] < min_duration:
                continue
            if max_duration is not None and row["duration_minutes"] > max_duration:
                continue
            if raw_filters["warnings_only"] and not warnings.get(row["id"]):
                continue
            drives.append(row)

        def display_item(
            row: sqlite3.Row,
            contribution: str | None = None,
            contribution_label: str | None = None,
        ) -> dict[str, object]:
            day_minutes = int(row["day_minutes"])
            night_minutes = int(row["night_minutes"])
            if contribution == "day":
                counted_day, counted_night = day_minutes, 0
            elif contribution == "night":
                counted_day, counted_night = 0, night_minutes
            else:
                counted_day, counted_night = day_minutes, night_minutes
            return {
                "row": row,
                "warnings": warnings.get(row["id"], []),
                "counted_minutes": counted_day + counted_night,
                "counted_day_minutes": counted_day,
                "counted_night_minutes": counted_night,
                "contribution_label": (
                    contribution_label if contribution and day_minutes and night_minutes else None
                ),
            }

        selected_sunlight = raw_filters["day_night"] or None
        enriched: list[dict[str, object]] = [
            display_item(
                row,
                selected_sunlight,
                "Daytime" if selected_sunlight == "day" else "Nighttime",
            )
            for row in drives
        ]
        totals = {
            "count": len(drives),
            "minutes": sum(cast(int, item["counted_minutes"]) for item in enriched),
            "day_minutes": sum(cast(int, item["counted_day_minutes"]) for item in enriched),
            "night_minutes": sum(cast(int, item["counted_night_minutes"]) for item in enriched),
        }
        groups: list[dict[str, object]] = []
        if group_by != "none":
            grouped: dict[str, dict[str, object]] = {}
            grouped_items = enriched
            if group_by == "day_night":
                grouped_items = []
                for row in drives:
                    if int(row["day_minutes"]) and selected_sunlight != "night":
                        grouped_items.append(display_item(row, "day", "Day"))
                    if int(row["night_minutes"]) and selected_sunlight != "day":
                        grouped_items.append(display_item(row, "night", "Night"))
            for item in grouped_items:
                row = cast(sqlite3.Row, item["row"])
                if group_by == "day_night":
                    key = "day" if cast(int, item["counted_day_minutes"]) else "night"
                    label = key.title()
                else:
                    key, label = _drive_group_key(row, group_by)
                group = grouped.setdefault(
                    key,
                    {
                        "key": key,
                        "label": label,
                        "drives": [],
                        "minutes": 0,
                        "day_minutes": 0,
                        "night_minutes": 0,
                    },
                )
                cast(list[dict[str, object]], group["drives"]).append(item)
                group["minutes"] = cast(int, group["minutes"]) + cast(int, item["counted_minutes"])
                group["day_minutes"] = cast(int, group["day_minutes"]) + cast(
                    int, item["counted_day_minutes"]
                )
                group["night_minutes"] = cast(int, group["night_minutes"]) + cast(
                    int, item["counted_night_minutes"]
                )
            groups = list(grouped.values())
            if group_by == "date":
                groups.sort(key=lambda group: str(group["key"]), reverse=True)
            elif group_by in {"part_of_day", "duration"}:
                order = {
                    "morning": 0,
                    "afternoon": 1,
                    "evening": 2,
                    "nighttime (dusk–dawn)": 3,
                    "under 30 minutes": 0,
                    "30–59 minutes": 1,
                    "1–2 hours": 2,
                    "2+ hours": 3,
                }
                groups.sort(
                    key=lambda group: (
                        (
                            0,
                            order[str(group["label"]).lower()],
                        )
                        if str(group["label"]).lower() in order
                        else (1, str(group["label"]).casefold())
                    )
                )
            else:
                groups.sort(key=lambda group: str(group["label"]).casefold())
        return templates.TemplateResponse(
            request,
            "drives.html",
            common(
                request,
                title="Drive history",
                drives=enriched,
                filters=raw_filters,
                weather_filters=weather_filters,
                supervisors=sorted(
                    {
                        str(row["supervisor_name"])
                        for row in records.list_drives()
                        if row["supervisor_name"]
                    },
                    key=str.casefold,
                ),
                weather_options=[
                    (value, label)
                    for value, label in WEATHER_LABELS
                    if any(
                        value in _weather_categories(row["weather"])
                        for row in records.list_drives()
                    )
                ],
                time_of_day_options=TIME_OF_DAY_OPTIONS,
                date_ranges={
                    "all": {"start": "", "end": ""},
                    "today": {"start": today.isoformat(), "end": today.isoformat()},
                    "week": {
                        "start": (today - timedelta(days=today.weekday())).isoformat(),
                        "end": today.isoformat(),
                    },
                    "last_week": {
                        "start": (today - timedelta(days=today.weekday() + 7)).isoformat(),
                        "end": (today - timedelta(days=today.weekday() + 1)).isoformat(),
                    },
                    "month": {"start": today.replace(day=1).isoformat(), "end": today.isoformat()},
                    "last_month": {
                        "start": (today.replace(day=1) - timedelta(days=1))
                        .replace(day=1)
                        .isoformat(),
                        "end": (today.replace(day=1) - timedelta(days=1)).isoformat(),
                    },
                    "year": {
                        "start": today.replace(month=1, day=1).isoformat(),
                        "end": today.isoformat(),
                    },
                    "last_year": {
                        "start": today.replace(year=today.year - 1, month=1, day=1).isoformat(),
                        "end": today.replace(year=today.year - 1, month=12, day=31).isoformat(),
                    },
                },
                totals=totals,
                groups=groups,
                group_by=group_by,
                group_by_options=GROUP_BY_OPTIONS,
            ),
        )

    @app.get("/drives/new", response_class=HTMLResponse)
    async def drive_new(request: Request) -> HTMLResponse:
        now = datetime.now(ZONE).replace(second=0, microsecond=0)
        return templates.TemplateResponse(
            request,
            "drive_form.html",
            common(
                request,
                title="Add drive",
                drive=None,
                start_local=_local_input_value(now),
                end_local=_local_input_value(now + timedelta(minutes=30)),
                action="/drives",
                **_duration_parts(30),
            ),
        )

    @app.post("/drives")
    async def drive_create(request: Request) -> RedirectResponse:
        form = await read_form(request)
        drive = records.create(
            DriveInput(
                driver_name=str(form.get("driver_name", "Daniel Ahern")),
                supervisor_name=str(form.get("supervisor_name", "")) or None,
                supervisor_dl_number=None,
                supervisor_dl_state=None,
                started_at_utc=_parse_local(
                    str(form["started_at_local"]), str(form.get("start_fold", ""))
                ),
                ended_at_utc=_parse_local(
                    str(form["ended_at_local"]), str(form.get("end_fold", ""))
                ),
                road_type=str(form.get("road_type", "unknown")),
                weather=str(form.get("weather", "")),
                notes=str(form.get("notes", "")),
                end_location=str(form.get("end_location", "")),
            ),
            request_id=str(form["request_id"]),
        )
        return redirect(f"/drives/{drive['id']}")

    @app.get("/drives/{drive_id}", response_class=HTMLResponse)
    async def drive_detail(request: Request, drive_id: str) -> HTMLResponse:
        drive = records.get(drive_id)
        return templates.TemplateResponse(
            request,
            "drive_detail.html",
            common(
                request,
                title="Drive details",
                drive=drive,
                warnings=records.warnings_for(drive_id),
                import_evidence=records.import_evidence_for(drive_id),
            ),
        )

    @app.get("/drives/{drive_id}/edit", response_class=HTMLResponse)
    async def drive_edit(request: Request, drive_id: str) -> HTMLResponse:
        drive = records.get(drive_id)
        return templates.TemplateResponse(
            request,
            "drive_form.html",
            common(
                request,
                title="Edit drive",
                drive=drive,
                start_local=_local_input_value(drive["started_at_utc"]),
                end_local=_local_input_value(drive["ended_at_utc"]),
                action=f"/drives/{drive_id}/edit",
                **_duration_parts(int(drive["duration_minutes"])),
            ),
        )

    @app.post("/drives/{drive_id}/edit")
    async def drive_update(request: Request, drive_id: str) -> RedirectResponse:
        form = await read_form(request)
        current = records.get(drive_id)
        start_text = str(form["started_at_local"])
        end_text = str(form["ended_at_local"])
        start_utc = (
            datetime.fromisoformat(str(current["started_at_utc"]).replace("Z", "+00:00"))
            if start_text == _local_input_value(current["started_at_utc"])
            else _parse_local(start_text, str(form.get("start_fold", "")))
        )
        end_utc = (
            datetime.fromisoformat(str(current["ended_at_utc"]).replace("Z", "+00:00"))
            if end_text == _local_input_value(current["ended_at_utc"])
            else _parse_local(end_text, str(form.get("end_fold", "")))
        )
        value = DriveInput(
            driver_name=str(form.get("driver_name", current["driver_name"])),
            supervisor_name=str(form.get("supervisor_name", "")) or None,
            supervisor_dl_number=current["supervisor_dl_number"],
            supervisor_dl_state=current["supervisor_dl_state"],
            started_at_utc=start_utc,
            ended_at_utc=end_utc,
            timezone_name=current["timezone_name"],
            road_type=str(form.get("road_type", "unknown")),
            weather=str(form.get("weather", "")),
            notes=str(form.get("notes", "")),
            end_location=str(form.get("end_location", "")),
            source=current["source"],
            source_reference=current["source_reference"],
            import_batch_id=current["import_batch_id"],
        )
        records.update(
            drive_id,
            value,
            expected_version=int(str(form["version"])),
            request_id=str(form["request_id"]),
        )
        return redirect(f"/drives/{drive_id}")

    @app.post("/drives/{drive_id}/delete")
    async def drive_delete(request: Request, drive_id: str) -> RedirectResponse:
        form = await read_form(request)
        records.delete(
            drive_id,
            expected_version=int(str(form["version"])),
            request_id=str(form["request_id"]),
        )
        return redirect("/drives")

    @app.get("/live", response_class=HTMLResponse)
    async def live_page(request: Request) -> HTMLResponse:
        open_drive = live.current()
        time_values: dict[str, object] = {}
        if open_drive and open_drive["status"] == "ending":
            started = datetime.fromisoformat(
                str(open_drive["started_at_utc"]).replace("Z", "+00:00")
            )
            ended = datetime.fromisoformat(
                str(open_drive["provisional_ended_at_utc"]).replace("Z", "+00:00")
            )
            minutes = round((ended - started).total_seconds() / 60)
            time_values = {
                "start_local": _local_input_value(started),
                "end_local": _local_input_value(ended),
                **_duration_parts(minutes),
            }
        return templates.TemplateResponse(
            request,
            "live.html",
            common(request, title="Live drive", live=open_drive, **time_values),
        )

    @app.get("/live/state")
    async def live_state() -> dict[str, object]:
        row = live.current()
        state: dict[str, object] = {"live": dict(row) if row else None, **_theme_context()}
        return state

    @app.post("/live/start")
    async def live_start(request: Request) -> RedirectResponse:
        form = await read_form(request)
        live.start(request_id=str(form["request_id"]), actor_identity=_actor(request))
        return redirect("/live")

    @app.post("/live/{live_id}/end")
    async def live_end(request: Request, live_id: str) -> RedirectResponse:
        form = await read_form(request)
        live.end(
            live_id,
            request_id=str(form["request_id"]),
            actor_identity=_actor(request),
        )
        return redirect("/live")

    @app.post("/live/{live_id}/resume")
    async def live_resume(request: Request, live_id: str) -> RedirectResponse:
        form = await read_form(request)
        live.resume(
            live_id,
            request_id=str(form["request_id"]),
            actor_identity=_actor(request),
        )
        return redirect("/live")

    @app.post("/live/{live_id}/cancel")
    async def live_cancel(request: Request, live_id: str) -> RedirectResponse:
        form = await read_form(request)
        live.cancel(
            live_id,
            request_id=str(form["request_id"]),
            actor_identity=_actor(request),
        )
        return redirect("/")

    @app.post("/live/{live_id}/finalize")
    async def live_finalize(request: Request, live_id: str) -> RedirectResponse:
        form = await read_form(request)
        current = live.find(live_id)
        if current["status"] not in ("ending", "completed"):
            raise ConflictError("live drive is no longer awaiting finalization")
        start_text = str(form["started_at_local"])
        end_text = str(form["ended_at_local"])
        corrected_start = (
            None
            if start_text == _local_input_value(current["started_at_utc"])
            else _parse_local(start_text, str(form.get("start_fold", "")))
        )
        corrected_end = (
            None
            if end_text == _local_input_value(current["provisional_ended_at_utc"])
            else _parse_local(end_text, str(form.get("end_fold", "")))
        )
        live.finalize(
            live_id,
            request_id=str(form["request_id"]),
            road_type=str(form.get("road_type", "unknown")),
            weather=str(form.get("weather", "")),
            notes=str(form.get("notes", "")),
            end_location=str(form.get("end_location", "")),
            supervisor_name=str(form.get("supervisor_name", "")) or None,
            corrected_start_utc=corrected_start,
            corrected_end_utc=corrected_end,
            actor_identity=_actor(request),
        )
        return redirect("/")

    @app.get("/imports", response_class=HTMLResponse)
    async def imports_page(request: Request) -> HTMLResponse:
        connection = database.connect_readonly()
        batches = connection.execute(
            "SELECT * FROM import_batches ORDER BY imported_at DESC"
        ).fetchall()
        connection.close()
        return templates.TemplateResponse(
            request,
            "imports.html",
            common(request, title="Imports and exports", batches=batches),
        )

    @app.get("/csv/export")
    async def csv_download() -> Response:
        return Response(
            export_csv(database),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="driving-log.csv"',
                "Cache-Control": "no-store",
            },
        )

    @app.post("/csv/import")
    async def csv_upload(request: Request) -> RedirectResponse:
        form = await read_form(request)
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise HTTPException(status_code=400, detail="CSV file is required")
        import_csv(database, await upload.read(), upload.filename or "upload.csv")
        return redirect("/imports")

    @app.get("/dmv", response_class=HTMLResponse)
    async def dmv_page(request: Request) -> HTMLResponse:
        stored_profiles = [
            {
                **dict(profile),
                "masked_license": mask_license(str(profile["dl_number"])),
            }
            for profile in profiles.list_profiles()
        ]
        return templates.TemplateResponse(
            request,
            "dmv.html",
            common(
                request,
                title="DMV driving record",
                profiles=stored_profiles,
                drive_supervisors=profiles.distinct_drive_names(),
                review=dmv.review(),
            ),
        )

    @app.post("/dmv/profiles")
    async def dmv_profile_save(request: Request) -> RedirectResponse:
        form = await read_form(request)
        profile_id = str(form.get("profile_id", "")).strip() or None
        expected_text = str(form.get("version", "")).strip()
        profiles.save(
            display_name=str(form["display_name"]),
            dl_number=str(form["dl_number"]),
            dl_state=str(form["dl_state"]),
            request_id=str(form["request_id"]),
            profile_id=profile_id,
            expected_version=int(expected_text) if expected_text else None,
            actor_identity=_actor(request),
        )
        return redirect("/dmv")

    @app.post("/dmv/profiles/{profile_id}/delete")
    async def dmv_profile_delete(request: Request, profile_id: str) -> RedirectResponse:
        form = await read_form(request)
        profiles.delete(
            profile_id,
            expected_version=int(str(form["version"])),
            request_id=str(form["request_id"]),
            actor_identity=_actor(request),
        )
        return redirect("/dmv")

    @app.get("/dmv/export")
    async def dmv_download() -> Response:
        return Response(
            dmv.generate(),
            media_type="application/pdf",
            headers={
                "Content-Disposition": 'attachment; filename="Daniel-driving-log-DL-4A.pdf"',
                "Cache-Control": "no-store",
            },
        )

    @app.get("/archives", response_class=HTMLResponse)
    async def archives_page(request: Request) -> HTMLResponse:
        archive_paths = sorted(
            settings.archive_dir.glob("*.tar.gz"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        archives = [
            {
                "path": path,
                "created_at": datetime.fromtimestamp(path.stat().st_mtime, UTC),
            }
            for path in archive_paths
        ]
        return templates.TemplateResponse(
            request,
            "archives.html",
            common(request, title="Archives", archives=archives),
        )

    @app.post("/archives")
    async def archive_create_web(request: Request) -> RedirectResponse:
        await read_form(request)
        try:
            create_archive(database, settings.archive_dir)
        except ArchiveSchemaTooNewError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return redirect("/archives")
