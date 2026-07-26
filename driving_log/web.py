from __future__ import annotations

import hashlib
import sqlite3
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
from driving_log.security import InvalidFormToken, create_form_token, verify_form_token
from driving_log.solar import (
    apex_daylight_window,
    resolve_local,
    split_day_night,
    week_segments,
)

PACKAGE_DIR = Path(__file__).parent
ZONE = ZoneInfo("America/New_York")


def _parse_local(value: str, fold_text: str | None = None) -> datetime:
    naive = datetime.fromisoformat(value)
    fold = int(fold_text) if fold_text in ("0", "1") else None
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
        f"{local.strftime('%b')} {local.day}, {local.year} at {hour}:{local.strftime('%M %p %Z')}"
    )


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


def _live_preview(
    database: Database,
    records: RecordService,
    row: sqlite3.Row,
    end: datetime | None = None,
) -> dict[str, object]:
    live_row = dict(row)
    start = datetime.fromisoformat(str(live_row["started_at_utc"]).replace("Z", "+00:00"))
    selected_end = end or datetime.fromisoformat(
        str(live_row["provisional_ended_at_utc"]).replace("Z", "+00:00")
    )
    duration = round((selected_end - start).total_seconds() / 60)
    day, night = split_day_night(start, selected_end)
    warnings: list[str] = []
    if duration > 300:
        warnings.append("Drive exceeds five hours.")
    if start.astimezone(ZONE).date() != selected_end.astimezone(ZONE).date():
        warnings.append("Drive crosses local midnight.")
    connection = database.connect()
    overlap_count = connection.execute(
        "SELECT COUNT(*) FROM drives WHERE deleted_at IS NULL "
        "AND started_at_utc < ? AND ended_at_utc > ?",
        (
            selected_end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        ),
    ).fetchone()[0]
    connection.close()
    if overlap_count:
        warnings.append(f"Drive overlaps {overlap_count} saved drive(s).")
    totals = records.totals()
    existing_weeks = cast(list[dict[str, object]], totals["weeks"])
    by_start: dict[str, int] = {}
    for week in existing_weeks:
        minutes = week["minutes"]
        assert isinstance(minutes, int)
        by_start[str(week["week_start_utc"])] = minutes
    for week_start, _week_end, minutes in week_segments(start, selected_end):
        if by_start.get(week_start.isoformat(), 0) + minutes > 600:
            warnings.append("Drive would put a calendar week over the 10-hour advisory.")
            break
    return {
        "duration_minutes": duration,
        "day_minutes": day,
        "night_minutes": night,
        "warnings": warnings,
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

    def token(action: str) -> str:
        return create_form_token(settings.form_secret, action)

    def common(request: Request, **values: object) -> dict[str, object]:
        return {
            "request": request,
            "token": token,
            "format_minutes": _format_minutes,
            "format_local_datetime": _format_local_datetime,
            "asset_version": asset_version,
            **_theme_context(),
            **values,
        }

    async def form_for(request: Request, action: str) -> FormData:
        host = request.headers.get("host", "")
        if host != settings.public_host:
            raise HTTPException(status_code=400, detail="invalid Host header")
        origin = request.headers.get("origin")
        expected_origin = f"https://{settings.public_host}"
        if settings.public_host.startswith(("127.0.0.1", "localhost", "testserver")):
            allowed_origins = {expected_origin, f"http://{settings.public_host}"}
        else:
            allowed_origins = {expected_origin}
        if origin not in allowed_origins:
            raise HTTPException(status_code=403, detail="cross-origin mutation rejected")
        content_type = request.headers.get("content-type", "").split(";", 1)[0]
        if content_type not in {
            "application/x-www-form-urlencoded",
            "multipart/form-data",
        }:
            raise HTTPException(status_code=415, detail="unsafe mutation content type")
        form = await request.form()
        try:
            verify_form_token(settings.form_secret, str(form.get("form_token", "")), action)
        except InvalidFormToken as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return form

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
        enriched = [{"row": row, "warnings": records.warnings_for(row["id"])} for row in drives]
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
                start_local=now.isoformat(timespec="minutes"),
                end_local=(now + timedelta(minutes=30)).isoformat(timespec="minutes"),
                action="/drives",
                form_action="drive.create",
            ),
        )

    @app.post("/drives")
    async def drive_create(request: Request) -> RedirectResponse:
        form = await form_for(request, "drive.create")
        drive = records.create(
            DriveInput(
                driver_name=str(form.get("driver_name", "Daniel Ahern")),
                supervisor_name=str(form.get("supervisor_name", "")) or None,
                supervisor_dl_number=str(form.get("supervisor_dl_number", "")) or None,
                supervisor_dl_state=str(form.get("supervisor_dl_state", "")) or None,
                started_at_utc=_parse_local(
                    str(form["started_at_local"]), str(form.get("start_fold", ""))
                ),
                ended_at_utc=_parse_local(
                    str(form["ended_at_local"]), str(form.get("end_fold", ""))
                ),
                road_type=str(form.get("road_type", "unknown")),
                weather=str(form.get("weather", "")),
                notes=str(form.get("notes", "")),
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
        start = datetime.fromisoformat(drive["started_at_utc"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(drive["ended_at_utc"].replace("Z", "+00:00"))
        return templates.TemplateResponse(
            request,
            "drive_form.html",
            common(
                request,
                title="Edit drive",
                drive=drive,
                start_local=start.astimezone(ZONE).isoformat(timespec="minutes"),
                end_local=end.astimezone(ZONE).isoformat(timespec="minutes"),
                action=f"/drives/{drive_id}/edit",
                form_action="drive.update",
            ),
        )

    @app.post("/drives/{drive_id}/edit")
    async def drive_update(request: Request, drive_id: str) -> RedirectResponse:
        form = await form_for(request, "drive.update")
        current = records.get(drive_id)
        value = DriveInput(
            driver_name=str(form.get("driver_name", current["driver_name"])),
            supervisor_name=str(form.get("supervisor_name", "")) or None,
            supervisor_dl_number=str(form.get("supervisor_dl_number", "")) or None,
            supervisor_dl_state=str(form.get("supervisor_dl_state", "")) or None,
            started_at_utc=_parse_local(
                str(form["started_at_local"]), str(form.get("start_fold", ""))
            ),
            ended_at_utc=_parse_local(str(form["ended_at_local"]), str(form.get("end_fold", ""))),
            timezone_name=current["timezone_name"],
            road_type=str(form.get("road_type", "unknown")),
            weather=str(form.get("weather", "")),
            notes=str(form.get("notes", "")),
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
        form = await form_for(request, "drive.delete")
        records.delete(
            drive_id,
            expected_version=int(str(form["version"])),
            request_id=str(form["request_id"]),
        )
        return redirect("/drives")

    @app.get("/live", response_class=HTMLResponse)
    async def live_page(request: Request) -> HTMLResponse:
        open_drive = live.current()
        preview = (
            _live_preview(database, records, open_drive)
            if open_drive and open_drive["status"] == "ending"
            else None
        )
        return templates.TemplateResponse(
            request,
            "live.html",
            common(request, title="Live drive", live=open_drive, preview=preview),
        )

    allowed_form_actions = {
        "drive.create",
        "drive.update",
        "drive.delete",
        "live.start",
        "live.end",
        "live.cancel",
        "live.resume",
        "live.finalize",
        "csv.import",
        "archive.create",
    }

    @app.get("/form-token/{action}")
    async def refresh_form_token(request: Request, action: str) -> dict[str, str]:
        if request.headers.get("host", "") != settings.public_host:
            raise HTTPException(status_code=400, detail="invalid Host header")
        if action not in allowed_form_actions:
            raise HTTPException(status_code=404, detail="unknown form action")
        return {"token": token(action)}

    @app.get("/live/state")
    async def live_state() -> dict[str, object]:
        row = live.current()
        state: dict[str, object] = {"live": dict(row) if row else None, **_theme_context()}
        if row:
            state["tokens"] = {
                action: token(f"live.{action}")
                for action in ("end", "cancel", "resume", "finalize")
            }
        return state

    @app.post("/live/start")
    async def live_start(request: Request) -> RedirectResponse:
        form = await form_for(request, "live.start")
        live.start(request_id=str(form["request_id"]), actor_identity=_actor(request))
        return redirect("/live")

    @app.post("/live/{live_id}/end")
    async def live_end(request: Request, live_id: str) -> RedirectResponse:
        form = await form_for(request, "live.end")
        live.end(
            live_id,
            request_id=str(form["request_id"]),
            actor_identity=_actor(request),
        )
        return redirect("/live")

    @app.post("/live/{live_id}/resume")
    async def live_resume(request: Request, live_id: str) -> RedirectResponse:
        form = await form_for(request, "live.resume")
        live.resume(
            live_id,
            request_id=str(form["request_id"]),
            actor_identity=_actor(request),
        )
        return redirect("/live")

    @app.post("/live/{live_id}/cancel")
    async def live_cancel(request: Request, live_id: str) -> RedirectResponse:
        form = await form_for(request, "live.cancel")
        live.cancel(
            live_id,
            request_id=str(form["request_id"]),
            actor_identity=_actor(request),
        )
        return redirect("/")

    @app.post("/live/{live_id}/finalize")
    async def live_finalize(request: Request, live_id: str) -> RedirectResponse:
        form = await form_for(request, "live.finalize")
        corrected_text = str(form.get("corrected_end_local", "")).strip()
        current = live.current()
        if not current or current["id"] != live_id:
            raise ConflictError("live drive is no longer awaiting finalization")
        corrected_end = _parse_local(corrected_text) if corrected_text else None
        preview = _live_preview(database, records, current, corrected_end)
        if preview["warnings"] and form.get("acknowledge_warnings") != "yes":
            raise HTTPException(status_code=400, detail="review and acknowledge drive warnings")
        drive = live.finalize(
            live_id,
            request_id=str(form["request_id"]),
            road_type=str(form.get("road_type", "unknown")),
            weather=str(form.get("weather", "")),
            notes=str(form.get("notes", "")),
            supervisor_name=str(form.get("supervisor_name", "")) or None,
            supervisor_dl_number=str(form.get("supervisor_dl_number", "")) or None,
            supervisor_dl_state=str(form.get("supervisor_dl_state", "")) or None,
            corrected_end_utc=corrected_end,
            actor_identity=_actor(request),
        )
        return redirect(f"/drives/{drive['id']}")

    @app.get("/imports", response_class=HTMLResponse)
    async def imports_page(request: Request) -> HTMLResponse:
        connection = database.connect()
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
        form = await form_for(request, "csv.import")
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
        await form_for(request, "archive.create")
        create_archive(database, settings.archive_dir)
        return redirect("/archives")
