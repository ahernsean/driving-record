from __future__ import annotations

import hashlib
import io
import math
import re
import sqlite3
import uuid
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject
from reportlab.pdfbase.pdfmetrics import stringWidth  # type: ignore[import-untyped]
from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]

from driving_log.db import Database, utc_now_text
from driving_log.records import ConflictError, NotFoundError, payload_hash

TEMPLATE_SHA256 = "b76eb3fe8b37041bdf9d34741b6cdefe01b213028a6b83d0504b501fd3a2fb5e"
PAGE_WIDTH = 612.0
PAGE_HEIGHT = 792.0
EXPECTED_FIRST_PAGE_ROWS = 20
EXPECTED_CONTINUATION_ROWS = 40
CONTINUATION_TABLE_TOP = 715.92
CONTINUATION_ROW_HEIGHT = 13.92


@dataclass(frozen=True)
class DmvRow:
    date: str
    day: str
    night: str
    total: str
    supervisor_name: str
    supervisor_license: str


@dataclass(frozen=True)
class DmvReview:
    drive_count: int
    page_count: int
    unmapped_supervisors: tuple[str, ...]
    blank_supervisor_count: int
    weekly_overages: tuple[dict[str, object], ...]


def normalize_supervisor_name(value: str) -> str:
    return " ".join(value.split()).casefold()


def mask_license(value: str) -> str:
    visible = value[-4:]
    hidden = max(4, len(value) - len(visible))
    return f"{'•' * hidden}{visible}"


def format_hours(minutes: int, *, blank_zero: bool = False) -> str:
    if minutes < 0:
        raise ValueError("duration cannot be negative")
    if blank_zero and minutes == 0:
        return ""
    hours, remaining = divmod(minutes, 60)
    return f"{hours}:{remaining:02d}"


class SupervisorProfileService:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _validated(display_name: str, dl_number: str, dl_state: str) -> tuple[str, str, str, str]:
        selected_name = " ".join(display_name.split())
        selected_number = dl_number.strip()
        selected_state = dl_state.strip().upper()
        normalized = normalize_supervisor_name(selected_name)
        if not selected_name or len(selected_name) > 120:
            raise ValueError("supervisor name is required and must be at most 120 characters")
        if not selected_number or len(selected_number) > 64:
            raise ValueError("license number is required and must be at most 64 characters")
        if not re.fullmatch(r"[A-Z]{2}", selected_state):
            raise ValueError("license state must be a two-letter abbreviation")
        return selected_name, normalized, selected_number, selected_state

    def list_profiles(self) -> list[sqlite3.Row]:
        connection = self.database.connect_readonly()
        rows = connection.execute(
            "SELECT * FROM supervisor_profiles ORDER BY display_name COLLATE NOCASE, id"
        ).fetchall()
        connection.close()
        return rows

    def distinct_drive_names(self) -> list[str]:
        connection = self.database.connect_readonly()
        rows = connection.execute(
            """
            SELECT supervisor_name
            FROM drives
            WHERE deleted_at IS NULL
              AND supervisor_name IS NOT NULL
              AND trim(supervisor_name) <> ''
            GROUP BY supervisor_name COLLATE NOCASE
            ORDER BY supervisor_name COLLATE NOCASE
            """
        ).fetchall()
        connection.close()
        return [str(row["supervisor_name"]) for row in rows]

    def get(self, profile_id: str, connection: sqlite3.Connection | None = None) -> sqlite3.Row:
        owned = connection is None
        selected = connection or self.database.connect_readonly()
        try:
            row = selected.execute(
                "SELECT * FROM supervisor_profiles WHERE id=?", (profile_id,)
            ).fetchone()
            if not row:
                raise NotFoundError(f"supervisor profile not found: {profile_id}")
            return cast(sqlite3.Row, row)
        finally:
            if owned:
                selected.close()

    def save(
        self,
        *,
        display_name: str,
        dl_number: str,
        dl_state: str,
        request_id: str,
        profile_id: str | None = None,
        expected_version: int | None = None,
        actor_identity: str | None = None,
    ) -> sqlite3.Row:
        name, normalized, number, state = self._validated(display_name, dl_number, dl_state)
        selected_id = profile_id or str(uuid.uuid4())
        action = "supervisor_profile.update" if profile_id else "supervisor_profile.create"
        digest = payload_hash(
            {
                "action": action,
                "profile_id": profile_id,
                "display_name": name,
                "dl_number": number,
                "dl_state": state,
                "expected_version": expected_version,
            }
        )
        with self.database.transaction() as connection:
            prior = connection.execute(
                "SELECT action, payload_hash, entity_id FROM audit_events WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if prior:
                if prior["action"] != action or prior["payload_hash"] != digest:
                    raise ConflictError("request ID was already used with different profile data")
                return self.get(str(prior["entity_id"]), connection)
            now = utc_now_text()
            if profile_id:
                current = self.get(profile_id, connection)
                if expected_version is None or current["version"] != expected_version:
                    raise ConflictError(
                        f"profile version is {current['version']}, expected {expected_version}"
                    )
                try:
                    changed = connection.execute(
                        """
                        UPDATE supervisor_profiles
                        SET display_name=?, normalized_name=?, dl_number=?, dl_state=?,
                            version=version+1, updated_at=?
                        WHERE id=? AND version=?
                        """,
                        (
                            name,
                            normalized,
                            number,
                            state,
                            now,
                            profile_id,
                            expected_version,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("a profile already exists for that supervisor") from exc
                if changed.rowcount != 1:
                    raise ConflictError("supervisor profile changed before this update")
            else:
                try:
                    connection.execute(
                        """
                        INSERT INTO supervisor_profiles
                        (id, display_name, normalized_name, dl_number, dl_state, version,
                         created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                        """,
                        (selected_id, name, normalized, number, state, now, now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ConflictError("a profile already exists for that supervisor") from exc
            self.database.audit(
                connection,
                event_id=str(uuid.uuid4()),
                request_id=request_id,
                action=action,
                payload_hash=digest,
                entity_type="supervisor_profile",
                entity_id=selected_id,
                outcome="updated" if profile_id else "created",
                metadata={"display_name": name},
                actor_identity=actor_identity,
            )
            return self.get(selected_id, connection)

    def delete(
        self,
        profile_id: str,
        *,
        expected_version: int,
        request_id: str,
        actor_identity: str | None = None,
    ) -> None:
        digest = payload_hash(
            {
                "action": "supervisor_profile.delete",
                "profile_id": profile_id,
                "expected_version": expected_version,
            }
        )
        with self.database.transaction() as connection:
            prior = connection.execute(
                "SELECT action, payload_hash FROM audit_events WHERE request_id=?", (request_id,)
            ).fetchone()
            if prior:
                if (
                    prior["action"] != "supervisor_profile.delete"
                    or prior["payload_hash"] != digest
                ):
                    raise ConflictError("request ID was already used for another mutation")
                return
            current = self.get(profile_id, connection)
            if current["version"] != expected_version:
                raise ConflictError(
                    f"profile version is {current['version']}, expected {expected_version}"
                )
            connection.execute("DELETE FROM supervisor_profiles WHERE id=?", (profile_id,))
            self.database.audit(
                connection,
                event_id=str(uuid.uuid4()),
                request_id=request_id,
                action="supervisor_profile.delete",
                payload_hash=digest,
                entity_type="supervisor_profile",
                entity_id=profile_id,
                outcome="deleted",
                metadata={"display_name": current["display_name"]},
                actor_identity=actor_identity,
            )


def _template_path() -> Path:
    resource = files("driving_log").joinpath("assets/DL-4A.pdf")
    return Path(str(resource))


def _template_bytes(template_path: Path | None = None) -> bytes:
    selected = template_path or _template_path()
    content = selected.read_bytes()
    if hashlib.sha256(content).hexdigest() != TEMPLATE_SHA256:
        raise ValueError("DL-4A template checksum does not match the supported layout")
    return content


def _annotation_array(page: DictionaryObject) -> ArrayObject:
    annotations = page.get("/Annots")
    if annotations is None:
        return ArrayObject()
    return cast(ArrayObject, annotations.get_object())


def _row_rectangles(page: DictionaryObject) -> list[list[tuple[float, float, float, float]]]:
    rectangles: list[tuple[float, float, float, float]] = []
    for reference in _annotation_array(page):
        annotation = reference.get_object()
        rect = tuple(float(value) for value in annotation["/Rect"])
        if len(rect) != 4:
            continue
        x0, y0, x1, y1 = rect
        if x0 < 590 and x1 > 30:
            rectangles.append((x0, y0, x1, y1))
    groups: list[list[tuple[float, float, float, float]]] = []
    for rect in sorted(rectangles, key=lambda item: -((item[1] + item[3]) / 2)):
        center = (rect[1] + rect[3]) / 2
        if groups:
            prior_center = sum((item[1] + item[3]) / 2 for item in groups[-1]) / len(groups[-1])
            if abs(center - prior_center) <= 0.6:
                groups[-1].append(rect)
                continue
        groups.append([rect])
    rows = [
        sorted(group, key=lambda rect: rect[0])
        for group in groups
        if len(group) == 6 and min(rect[0] for rect in group) < 50
    ]
    return rows


def _continuation_rectangles(
    widget_rows: list[list[tuple[float, float, float, float]]],
) -> list[list[tuple[float, float, float, float]]]:
    """Return rectangles for every printed continuation row.

    The official PDF has only 36 form-field rows over a printed 40-row grid,
    and those widgets drift out of alignment with the grid. The template hash
    pins this geometry; widget columns still provide the horizontal layout.
    """
    if not widget_rows or len(widget_rows[0]) != 6:
        return []
    columns = [(rect[0], rect[2]) for rect in widget_rows[0]]
    rows: list[list[tuple[float, float, float, float]]] = []
    for index in range(EXPECTED_CONTINUATION_ROWS):
        y1 = CONTINUATION_TABLE_TOP - (index * CONTINUATION_ROW_HEIGHT)
        y0 = y1 - CONTINUATION_ROW_HEIGHT
        rows.append([(x0, y0, x1, y1) for x0, x1 in columns])
    return rows


def _fit_text(canvas: Canvas, text: str, rect: tuple[float, float, float, float]) -> None:
    if not text:
        return
    x0, y0, x1, y1 = rect
    maximum_width = max(1.0, x1 - x0 - 3)
    size = min(7.4, y1 - y0 - 3)
    while size > 5.0 and stringWidth(text, "Helvetica", size) > maximum_width:
        size -= 0.2
    if stringWidth(text, "Helvetica", size) > maximum_width:
        while text and stringWidth(f"{text}…", "Helvetica", size) > maximum_width:
            text = text[:-1]
        text = f"{text}…"
    canvas.setFont("Helvetica", size)
    canvas.drawCentredString((x0 + x1) / 2, y0 + ((y1 - y0 - size) / 2) + 1.2, text)


def _overlay(
    rows: list[DmvRow],
    row_rectangles: list[list[tuple[float, float, float, float]]],
    *,
    page_number: int,
    page_count: int,
    final_page: bool,
    totals: tuple[str, str, str],
) -> bytes:
    output = io.BytesIO()
    canvas = Canvas(
        output,
        pagesize=(PAGE_WIDTH, PAGE_HEIGHT),
        pageCompression=1,
        invariant=1,
    )
    for row, rectangles in zip(rows, row_rectangles, strict=False):
        for text, rect in zip(
            (
                row.date,
                row.day,
                row.night,
                row.total,
                row.supervisor_name,
                row.supervisor_license,
            ),
            rectangles,
            strict=True,
        ):
            _fit_text(canvas, text, rect)
    canvas.setFillColorRGB(1, 1, 1)
    canvas.rect(245, 7, 125, 24, fill=1, stroke=0)
    canvas.setFillColorRGB(0, 0, 0)
    canvas.setFont("Helvetica", 7)
    canvas.drawCentredString(307.5, 14, f"{page_number} of {page_count}")
    if page_number > 1 and not final_page:
        canvas.setFillColorRGB(1, 1, 1)
        canvas.rect(28, 112, 558, 45, fill=1, stroke=0)
    if final_page:
        total_rectangles = (
            (152.16, 127.64, 199.92, 150.2),
            (339.72, 127.64, 388.2, 150.2),
            (491.52, 127.64, 545.76, 150.2),
        )
        for text, rect in zip(totals, total_rectangles, strict=True):
            _fit_text(canvas, text, rect)
    canvas.save()
    return output.getvalue()


class DmvExportService:
    def __init__(self, database: Database, template_path: Path | None = None):
        self.database = database
        self.template_path = template_path

    @staticmethod
    def _profiles(connection: sqlite3.Connection) -> dict[str, sqlite3.Row]:
        return {
            str(row["normalized_name"]): row
            for row in connection.execute("SELECT * FROM supervisor_profiles").fetchall()
        }

    def _rows(self, connection: sqlite3.Connection) -> list[DmvRow]:
        profiles = self._profiles(connection)
        drives = connection.execute(
            """
            SELECT * FROM drives
            WHERE deleted_at IS NULL
            ORDER BY started_at_utc, id
            """
        ).fetchall()
        output: list[DmvRow] = []
        for drive in drives:
            supervisor = " ".join(str(drive["supervisor_name"] or "").split())
            license_text = ""
            if supervisor:
                profile = profiles.get(normalize_supervisor_name(supervisor))
                if profile:
                    license_text = f"{profile['dl_number']}, {profile['dl_state']}"
                elif drive["supervisor_dl_number"] and drive["supervisor_dl_state"]:
                    license_text = (
                        f"{str(drive['supervisor_dl_number']).strip()}, "
                        f"{str(drive['supervisor_dl_state']).strip().upper()}"
                    )
            from datetime import datetime

            started = datetime.fromisoformat(
                str(drive["started_at_utc"]).replace("Z", "+00:00")
            ).astimezone(ZoneInfo(str(drive["timezone_name"])))
            output.append(
                DmvRow(
                    date=f"{started:%m/%d/%Y}",
                    day=format_hours(int(drive["day_minutes"]), blank_zero=True),
                    night=format_hours(int(drive["night_minutes"]), blank_zero=True),
                    total=format_hours(int(drive["duration_minutes"])),
                    supervisor_name=supervisor,
                    supervisor_license=license_text,
                )
            )
        return output

    def rows(self) -> list[DmvRow]:
        connection = self.database.connect_readonly()
        try:
            connection.execute("BEGIN")
            return self._rows(connection)
        finally:
            connection.close()

    @staticmethod
    def page_count(row_count: int) -> int:
        remaining = max(0, row_count - EXPECTED_FIRST_PAGE_ROWS)
        continuation_pages = max(1, math.ceil(remaining / EXPECTED_CONTINUATION_ROWS))
        return 1 + continuation_pages

    def review(self) -> DmvReview:
        connection = self.database.connect_readonly()
        profiles = self._profiles(connection)
        drives = connection.execute(
            "SELECT supervisor_name, supervisor_dl_number, supervisor_dl_state "
            "FROM drives WHERE deleted_at IS NULL"
        ).fetchall()
        connection.close()
        missing = 0
        unmapped: set[str] = set()
        for drive in drives:
            supervisor = " ".join(str(drive["supervisor_name"] or "").split())
            if not supervisor:
                missing += 1
            elif normalize_supervisor_name(supervisor) not in profiles and not (
                drive["supervisor_dl_number"] and drive["supervisor_dl_state"]
            ):
                unmapped.add(supervisor)
        from driving_log.records import RecordService

        totals = RecordService(self.database).totals()
        weeks = cast(list[dict[str, object]], totals["weeks"])
        weekly = tuple(week for week in weeks if cast(int, week["overage_minutes"]) > 0)
        count = len(drives)
        return DmvReview(
            drive_count=count,
            page_count=self.page_count(count),
            unmapped_supervisors=tuple(sorted(unmapped, key=str.casefold)),
            blank_supervisor_count=missing,
            weekly_overages=weekly,
        )

    def generate(self) -> bytes:
        template = _template_bytes(self.template_path)
        layout_reader = PdfReader(io.BytesIO(template))
        first_layout = _row_rectangles(cast(DictionaryObject, layout_reader.pages[0]))
        continuation_widgets = _row_rectangles(cast(DictionaryObject, layout_reader.pages[1]))
        continuation_layout = _continuation_rectangles(continuation_widgets)
        if (
            len(first_layout) != EXPECTED_FIRST_PAGE_ROWS
            or len(continuation_layout) != EXPECTED_CONTINUATION_ROWS
        ):
            raise ValueError("DL-4A template row layout is unsupported")
        connection = self.database.connect_readonly()
        try:
            connection.execute("BEGIN")
            rows = self._rows(connection)
            totals_row = connection.execute(
                """
                SELECT COALESCE(SUM(day_minutes), 0), COALESCE(SUM(night_minutes), 0),
                       COALESCE(SUM(duration_minutes), 0)
                FROM drives WHERE deleted_at IS NULL
                """
            ).fetchone()
        finally:
            connection.close()
        page_count = self.page_count(len(rows))
        totals = tuple(format_hours(int(value)) for value in totals_row)
        writer = PdfWriter()
        offset = 0
        for page_number in range(1, page_count + 1):
            first = page_number == 1
            template_index = 0 if first else 1
            layout = first_layout if first else continuation_layout
            page_reader = PdfReader(io.BytesIO(template))
            page = writer.add_page(page_reader.pages[template_index])
            selected_rows = rows[offset : offset + len(layout)]
            offset += len(selected_rows)
            overlay = PdfReader(
                io.BytesIO(
                    _overlay(
                        selected_rows,
                        layout,
                        page_number=page_number,
                        page_count=page_count,
                        final_page=page_number == page_count,
                        totals=cast(tuple[str, str, str], totals),
                    )
                )
            ).pages[0]
            page.merge_page(overlay)
            if "/Annots" in page:
                del page["/Annots"]
        if offset != len(rows):
            raise RuntimeError("not all drives fit in the generated DMV form")
        writer.root_object.pop("/AcroForm", None)
        writer.root_object.pop("/OpenAction", None)
        writer.root_object.pop("/Names", None)
        output = io.BytesIO()
        writer.write(output)
        return output.getvalue()
