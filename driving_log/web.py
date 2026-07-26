from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData, UploadFile
from starlette.middleware.base import RequestResponseEndpoint

from driving_log.archive import create_archive
from driving_log.config import Settings
from driving_log.csv_backup import export_csv, import_csv
from driving_log.db import Database
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


def _local_input_value(value: str | datetime) -> str:
    return _local_datetime(value).strftime("%Y-%m-%dT%H:%M")


def _duration_parts(minutes: int) -> dict[str, int]:
    return {
        "duration_hours": minutes // 60,
        "duration_remainder": minutes % 60,
    }


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
        drives = records.list_drives()
        warnings = records.warnings_for_many()
        enriched = [{"row": row, "warnings": warnings.get(row["id"], [])} for row in drives]
        return templates.TemplateResponse(
            request,
            "drives.html",
            common(request, title="Drive history", drives=enriched),
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
        drive = live.finalize(
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
        return redirect(f"/drives/{drive['id']}")

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
        create_archive(database, settings.archive_dir)
        return redirect("/archives")
